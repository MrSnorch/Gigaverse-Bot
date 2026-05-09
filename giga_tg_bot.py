#!/usr/bin/env python3
"""
Gigaverse Telegram control bot for GitHub Actions.

Modes:
  bot      - Telegram long polling: commands, settings, pinned status.
  worker   - One user worker: matrix runner that can start/play runs.
  matrix   - Build GitHub Actions matrix from active Supabase users.

Secrets expected in GitHub Actions:
  TELEGRAM_BOT_TOKEN
  SUPABASE_URL
  SUPABASE_SERVICE_KEY
  GIGA_SECRET_KEY        # Optional. If set, bearer tokens are encrypted before saving to Supabase.
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import requests
from cryptography.fernet import Fernet, InvalidToken


LOG = logging.getLogger("giga-tg")
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TG_API = f"https://api.telegram.org/bot{TG_TOKEN}" if TG_TOKEN else ""
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
DEFAULT_BASE_URL = (os.environ.get("GIGAVERSE_BASE_URL") or "https://gigaverse.io").rstrip("/")

MOVES = ("rock", "paper", "scissor")
MOVE_LABELS = {"rock": "Sword", "paper": "Shield", "scissor": "Magic"}
LABEL_TO_MOVE = {"sword": "rock", "shield": "paper", "magic": "scissor"}
COUNTERS = {"rock": "paper", "paper": "scissor", "scissor": "rock"}
LOSES_TO = {winner: loser for loser, winner in COUNTERS.items()}
LOOT_ACTIONS = ("loot_one", "loot_two", "loot_three")
RUN_TABLE = "giga_users"
SECRET_TABLE = "giga_user_secrets"
DEBUG_TABLE = "giga_debug_runs"
TURN_TABLE = "giga_debug_turns"
STATE_TABLE = "giga_bot_state"


DEFAULT_SETTINGS: dict[str, Any] = {
    "base_url": DEFAULT_BASE_URL,
    "dungeon_id": 1,
    "item_id": 0,
    "item_index": 0,
    "expected_amount": 0,
    "is_juiced": False,
    "gear_instance_ids": [],
    "runs_to_play": 1,
    "auto_continue": False,
    "move_delay_sec": 0.35,
    "loop_interval_sec": 2.0,
    "boss_room": 16,
    "loot_priority": "health",
}


DEFAULT_STATE: dict[str, Any] = {
    "status_msg_id": None,
    "action_token": "",
    "awaiting": "",
    "command": None,
    "current_run": None,
    "runs_remaining": 0,
    "last_move": "",
    "last_streak": 0,
    "enemy_history": [],
    "move_cooldowns": [],
    "activity_log": [],
    "last_run_summary": {},
    "debug": None,
    "last_error": "",
    "last_status_at": "",
}


class ApiError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, payload: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload or {}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def e(text: Any) -> str:
    return html.escape(str(text if text is not None else ""), quote=False)


def compact_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def deep_merge(defaults: dict[str, Any], payload: dict[str, Any] | None) -> dict[str, Any]:
    result = dict(defaults)
    for key, value in (payload or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def fernet() -> Fernet | None:
    key = os.environ.get("GIGA_SECRET_KEY", "").strip()
    if not key:
        return None
    return Fernet(key.encode("utf-8"))


def encrypt_secret(value: str) -> str:
    cipher = fernet()
    if not cipher:
        return "plain:" + value
    return "fernet:" + cipher.encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt_secret(value: str) -> str:
    if not value:
        return ""
    if value.startswith("plain:"):
        return value.removeprefix("plain:")
    if value.startswith("fernet:"):
        cipher = fernet()
        if not cipher:
            raise RuntimeError("Bearer token was encrypted, but GIGA_SECRET_KEY is not set.")
        try:
            return cipher.decrypt(value.removeprefix("fernet:").encode("utf-8")).decode("utf-8")
        except InvalidToken as exc:
            raise RuntimeError("Cannot decrypt bearer token. Check GIGA_SECRET_KEY.") from exc
    cipher = fernet()
    if not cipher:
        return value
    try:
        return cipher.decrypt(value.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        return value


def normalize_bearer_token(raw: str) -> str:
    value = raw.strip().strip('"').strip("'")
    if value.lower().startswith("authorization:"):
        value = value.split(":", 1)[1].strip()
    if value.lower().startswith("bearer "):
        value = value.split(None, 1)[1].strip()
    return value.strip().strip('"').strip("'")


def sb_headers(*, prefer: str = "return=representation") -> dict[str, str]:
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError("SUPABASE_URL or SUPABASE_SERVICE_KEY is missing.")
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": prefer,
    }


def sb_url(table: str) -> str:
    return f"{SUPABASE_URL}/rest/v1/{table}"


def sb_get(table: str, params: dict[str, str] | None = None) -> list[dict[str, Any]]:
    response = requests.get(sb_url(table), headers=sb_headers(), params=params or {}, timeout=20)
    if not response.ok:
        raise ApiError(f"Supabase GET {table}: {response.status_code} {response.text[:300]}")
    return response.json()


def sb_post(table: str, payload: Any, *, prefer: str = "return=representation") -> list[dict[str, Any]]:
    response = requests.post(sb_url(table), headers=sb_headers(prefer=prefer), json=payload, timeout=20)
    if not response.ok:
        raise ApiError(f"Supabase POST {table}: {response.status_code} {response.text[:300]}")
    return response.json() if response.text else []


def sb_patch(table: str, params: dict[str, str], payload: dict[str, Any]) -> bool:
    response = requests.patch(sb_url(table), headers=sb_headers(), params=params, json=payload, timeout=20)
    if not response.ok:
        raise ApiError(f"Supabase PATCH {table}: {response.status_code} {response.text[:300]}")
    return True


def get_user(telegram_id: int) -> dict[str, Any] | None:
    rows = sb_get(RUN_TABLE, {"telegram_id": f"eq.{telegram_id}", "select": "*"})
    if not rows:
        return None
    user = rows[0]
    secret = get_user_secret(telegram_id)
    user.update(secret)
    return user


def upsert_user(telegram_id: int, **fields: Any) -> dict[str, Any]:
    payload = {"telegram_id": telegram_id, **fields}
    rows = sb_post(
        f"{RUN_TABLE}?on_conflict=telegram_id",
        payload,
        prefer="return=representation,resolution=merge-duplicates",
    )
    return rows[0] if rows else payload


def update_user(telegram_id: int, **fields: Any) -> bool:
    return sb_patch(RUN_TABLE, {"telegram_id": f"eq.{telegram_id}"}, fields)


def get_user_secret(telegram_id: int) -> dict[str, str]:
    rows = sb_get(SECRET_TABLE, {"telegram_id": f"eq.{telegram_id}", "select": "bearer_token,encrypted_bearer_token"})
    if not rows:
        return {"bearer_token": "", "encrypted_bearer_token": ""}
    return {
        "bearer_token": str(rows[0].get("bearer_token") or ""),
        "encrypted_bearer_token": str(rows[0].get("encrypted_bearer_token") or ""),
    }


def save_user_secret(telegram_id: int, bearer_token: str) -> None:
    sb_post(
        f"{SECRET_TABLE}?on_conflict=telegram_id",
        {"telegram_id": telegram_id, "bearer_token": bearer_token, "encrypted_bearer_token": ""},
        prefer="return=minimal,resolution=merge-duplicates",
    )


def stored_bearer_value(user: dict[str, Any]) -> str:
    secret = get_user_secret(int(user["telegram_id"]))
    return str(secret.get("encrypted_bearer_token") or secret.get("bearer_token") or "")


def ensure_user(message_from: dict[str, Any]) -> dict[str, Any]:
    telegram_id = int(message_from.get("id"))
    user = get_user(telegram_id)
    settings = deep_merge(DEFAULT_SETTINGS, (user or {}).get("settings") or {})
    state = deep_merge(DEFAULT_STATE, (user or {}).get("state") or {})
    fields = {
        "username": str(message_from.get("username") or ""),
        "first_name": str(message_from.get("first_name") or ""),
        "settings": settings,
        "state": state,
    }
    if not user:
        fields["active"] = False
    return upsert_user(telegram_id, **fields)


def list_active_users() -> list[dict[str, Any]]:
    return sb_get(RUN_TABLE, {"active": "eq.true", "select": "telegram_id"})


def get_bot_offset() -> int:
    rows = sb_get(STATE_TABLE, {"key": "eq.telegram_offset", "select": "*"})
    if not rows:
        return 0
    value = rows[0].get("value") or {}
    return int(value.get("offset") or 0)


def save_bot_offset(offset: int) -> None:
    sb_post(
        f"{STATE_TABLE}?on_conflict=key",
        {"key": "telegram_offset", "value": {"offset": int(offset)}},
        prefer="return=representation,resolution=merge-duplicates",
    )


def save_debug_run(telegram_id: int, payload: dict[str, Any]) -> None:
    safe = sanitize_debug(payload)
    rows = sb_post(DEBUG_TABLE, {"telegram_id": telegram_id, **safe}, prefer="return=representation")
    run_row_id = int((rows[0] if rows else {}).get("id") or 0)
    if run_row_id:
        save_debug_turns(
            telegram_id=telegram_id,
            run_row_id=run_row_id,
            external_run_id=str(safe.get("external_run_id") or ""),
            turns=payload.get("combat_log") or [],
        )


def save_debug_turns(telegram_id: int, run_row_id: int, external_run_id: str, turns: list[dict[str, Any]]) -> None:
    rows: list[dict[str, Any]] = []
    for index, turn in enumerate(turns, start=1):
        compact = compact_turn(turn)
        rows.append(
            {
                "telegram_id": telegram_id,
                "run_row_id": run_row_id,
                "external_run_id": external_run_id,
                "turn_index": index,
                "room": compact.get("room"),
                "floor": compact.get("floor"),
                "enemy_id": compact.get("enemy_id"),
                "our_move": compact.get("our_move"),
                "enemy_move": compact.get("enemy_move"),
                "result": compact.get("result"),
                "before_state": compact.get("before") or {},
                "after_state": compact.get("after") or {},
                "decision": compact.get("decision") or {},
            }
        )
    for start in range(0, len(rows), 100):
        sb_post(TURN_TABLE, rows[start : start + 100], prefer="return=minimal")


def compact_turn(turn: dict[str, Any]) -> dict[str, Any]:
    decision = turn.get("decision") or {}
    scores = decision.get("scores") or {}
    compact_scores = {move: round(float(score), 3) for move, score in scores.items() if move in MOVES}
    return {
        "at": turn.get("at"),
        "room": turn.get("room"),
        "floor": turn.get("floor"),
        "enemy_id": turn.get("enemy_id"),
        "our_move": turn.get("our_move"),
        "enemy_move": turn.get("enemy_move"),
        "result": turn.get("result"),
        "before": turn.get("before") or {},
        "after": turn.get("after") or {},
        "decision": {
            "scores": compact_scores,
            "recent": decision.get("recent"),
            "predicted": decision.get("predicted"),
            "predicted_confidence": decision.get("predicted_confidence"),
            "enemy_available": decision.get("enemy_available") or [],
            "magic_underbuilt": bool(decision.get("magic_underbuilt")),
            "projection": decision.get("projection") or {},
        },
    }


def sanitize_debug(payload: dict[str, Any]) -> dict[str, Any]:
    settings = dict(payload.get("settings_snapshot") or {})
    settings.pop("bearer_token", None)
    settings.pop("encrypted_bearer_token", None)
    return {
        "external_run_id": str(payload.get("external_run_id") or ""),
        "started_at": payload.get("started_at") or utc_now(),
        "ended_at": payload.get("ended_at") or utc_now(),
        "status": str(payload.get("status") or "unknown"),
        "rooms_cleared": int(payload.get("rooms_cleared") or 0),
        "wins": int(payload.get("wins") or 0),
        "losses": int(payload.get("losses") or 0),
        "draws": int(payload.get("draws") or 0),
        "loot": payload.get("loot") or [],
        "drops": payload.get("drops") or [],
        "loot_value": payload.get("loot_value") or {},
        "enemy_report": payload.get("enemy_report") or {},
        "combat_log": [compact_turn(turn) for turn in (payload.get("combat_log") or [])[-12:]],
        "account_snapshot": payload.get("account_snapshot") or {},
        "settings_snapshot": settings,
    }


def tg(method: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    if not TG_API:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing.")
    response = requests.post(f"{TG_API}/{method}", json=payload or {}, timeout=20)
    if not response.ok:
        LOG.warning("Telegram %s failed: %s %s", method, response.status_code, response.text[:300])
        return {}
    return response.json()


def tg_get(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    response = requests.get(f"{TG_API}/{method}", params=params or {}, timeout=35)
    if not response.ok:
        LOG.warning("Telegram GET %s failed: %s %s", method, response.status_code, response.text[:300])
        return {}
    return response.json()


def send(chat_id: int, text: str, *, reply_markup: dict[str, Any] | None = None, silent: bool = False) -> int | None:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "disable_notification": silent,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    data = tg("sendMessage", payload)
    return ((data.get("result") or {}).get("message_id")) if data.get("ok") else None


def edit(chat_id: int, message_id: int, text: str, *, reply_markup: dict[str, Any] | None = None) -> bool:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    data = tg("editMessageText", payload)
    return bool(data.get("ok"))


def delete_message(chat_id: int, message_id: int) -> None:
    tg("deleteMessage", {"chat_id": chat_id, "message_id": message_id})


def answer_callback(callback_id: str, text: str = "") -> None:
    tg("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:200]})


def pin_message(chat_id: int, message_id: int) -> None:
    tg("pinChatMessage", {"chat_id": chat_id, "message_id": message_id, "disable_notification": True})


def dispatch_matrix_for_user(telegram_id: int) -> None:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    ref = os.environ.get("GITHUB_REF_NAME") or os.environ.get("GITHUB_REF") or "main"
    if not token or not repo:
        LOG.info("GitHub dispatch is not configured; matrix will start by schedule/manual run.")
        return
    if ref.startswith("refs/heads/"):
        ref = ref.removeprefix("refs/heads/")
    url = f"https://api.github.com/repos/{repo}/actions/workflows/giga_matrix.yml/dispatches"
    payload = {"ref": ref, "inputs": {"single_user": str(telegram_id)}}
    response = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        json=payload,
        timeout=20,
    )
    if not response.ok:
        LOG.warning("Matrix dispatch failed: %s %s", response.status_code, response.text[:300])


def main_keyboard() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "Start 1 run", "callback_data": "run:1"},
                {"text": "Start batch", "callback_data": "run:batch"},
            ],
            [
                {"text": "Refresh status", "callback_data": "status"},
                {"text": "Stop", "callback_data": "stop"},
            ],
            [
                {"text": "Set bearer token", "callback_data": "setup:token"},
                {"text": "Settings", "callback_data": "settings"},
            ],
        ]
    }


def settings_keyboard(settings: dict[str, Any]) -> dict[str, Any]:
    auto = "on" if settings.get("auto_continue") else "off"
    loot = str(settings.get("loot_priority") or "health")
    return {
        "inline_keyboard": [
            [
                {"text": f"Auto continue: {auto}", "callback_data": "toggle:auto"},
                {"text": f"Loot: {loot}", "callback_data": "toggle:loot"},
            ],
            [
                {"text": "Run 1", "callback_data": "run:1"},
                {"text": "Run saved batch", "callback_data": "run:saved"},
            ],
            [
                {"text": "Set bearer token", "callback_data": "setup:token"},
            ],
            [
                {"text": "Back", "callback_data": "home"},
            ],
        ]
    }


@dataclass
class GigaverseClient:
    token: str
    settings: dict[str, Any]
    state: dict[str, Any]

    @property
    def base_url(self) -> str:
        return str(self.settings.get("base_url") or DEFAULT_BASE_URL).rstrip("/")

    def headers(self, *, json_body: bool = False) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "GigaverseTelegramActionsBot/0.1",
            "Authorization": f"Bearer {self.token}",
        }
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        url = self.base_url + path
        try:
            response = requests.request(
                method,
                url,
                headers=self.headers(json_body=body is not None),
                json=body,
                timeout=35,
            )
        except requests.RequestException as exc:
            raise ApiError(f"{method} {path} failed: {exc}") from exc
        if not response.ok:
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            message = payload.get("message") or response.text[:300] or response.reason
            raise ApiError(f"{method} {path} failed: {message}", status_code=response.status_code, payload=payload)
        try:
            payload = response.json()
        except ValueError as exc:
            raise ApiError(f"{method} {path} returned invalid JSON") from exc
        token = payload.get("actionToken")
        if token:
            self.state["action_token"] = str(token)
        return payload

    def get_user_me(self) -> dict[str, Any]:
        return self.request("GET", "/api/user/me")

    def get_account(self, address: str) -> dict[str, Any]:
        return self.request("GET", f"/api/account/{address}")

    def get_energy(self, address: str) -> dict[str, Any]:
        return self.request("GET", f"/api/offchain/player/energy/{address}")

    def get_marketplace_floor_all(self) -> dict[str, Any]:
        return self.request("GET", "/api/marketplace/item/floor/all")

    def get_dungeon_state(self) -> dict[str, Any]:
        return self.request("GET", "/api/game/dungeon/state")

    def action_data(self) -> dict[str, Any]:
        return {
            "consumables": [],
            "itemId": int(self.settings.get("item_id") or 0),
            "expectedAmount": int(self.settings.get("expected_amount") or 0),
            "index": int(self.settings.get("item_index") or 0),
            "isJuiced": bool(self.settings.get("is_juiced")),
            "gearInstanceIds": list(self.settings.get("gear_instance_ids") or []),
        }

    def dungeon_action(self, action: str) -> dict[str, Any]:
        body = {
            "action": action,
            "actionToken": str(self.state.get("action_token") or ""),
            "dungeonId": int(self.settings.get("dungeon_id") or 1),
            "data": self.action_data(),
        }
        return self.request("POST", "/api/game/dungeon/action", body)

    def start_run(self) -> dict[str, Any]:
        return self.dungeon_action("start_run")


def move_label(move: str | None) -> str:
    return MOVE_LABELS.get(str(move or "").lower(), str(move or "-"))


def move_from_label(value: str) -> str:
    raw = value.strip().lower()
    return LABEL_TO_MOVE.get(raw, raw if raw in MOVES else "")


def health(player: dict[str, Any]) -> tuple[int, int]:
    h = player.get("health") or {}
    return int(h.get("current") or 0), int(h.get("currentMax") or 0)


def shield(player: dict[str, Any]) -> tuple[int, int]:
    s = player.get("shield") or {}
    return int(s.get("current") or 0), int(s.get("currentMax") or 0)


def move_stats(player: dict[str, Any], move: str) -> dict[str, int]:
    data = player.get(move) or {}
    return {
        "atk": int(data.get("currentATK") or 0),
        "def": int(data.get("currentDEF") or 0),
        "charges": max(0, int(data.get("currentCharges") or 0)),
    }


def effective_hp(player: dict[str, Any]) -> int:
    hp, _ = health(player)
    sh, _ = shield(player)
    return hp + sh


def outcome(our_move: str, enemy_move: str | None) -> str:
    if enemy_move not in MOVES:
        return "draw"
    if our_move == enemy_move:
        return "draw"
    return "win" if COUNTERS.get(enemy_move) == our_move else "loss"


def project_exchange(me: dict[str, Any], enemy: dict[str, Any], our_move: str, enemy_move: str) -> dict[str, Any]:
    result = outcome(our_move, enemy_move)
    our_hp = effective_hp(me)
    enemy_hp = effective_hp(enemy)
    our_move_stats = move_stats(me, our_move)
    enemy_move_stats = move_stats(enemy, enemy_move)
    our_sh, our_sh_max = shield(me)
    enemy_sh, enemy_sh_max = shield(enemy)
    our_gain = min(our_move_stats["def"], max(0, our_sh_max - our_sh)) if result in {"win", "draw"} else 0
    enemy_gain = min(enemy_move_stats["def"], max(0, enemy_sh_max - enemy_sh)) if result in {"loss", "draw"} else 0
    damage_to_enemy = our_move_stats["atk"] if result in {"win", "draw"} else 0
    damage_to_us = enemy_move_stats["atk"] if result in {"loss", "draw"} else 0
    return {
        "outcome": result,
        "our_end_eff": max(0, our_hp + our_gain - damage_to_us),
        "enemy_end_eff": max(0, enemy_hp + enemy_gain - damage_to_enemy),
    }


def enemy_available_moves(enemy: dict[str, Any]) -> list[str]:
    available = [move for move in MOVES if move_stats(enemy, move)["charges"] > 0]
    return available or list(MOVES)


def dominant_response(available: list[str]) -> str | None:
    if len(available) == 1:
        return COUNTERS.get(available[0])
    missing = [move for move in MOVES if move not in available]
    if len(missing) == 1:
        return LOSES_TO.get(missing[0])
    return None


def predict_enemy(history: list[str]) -> tuple[str | None, float]:
    recent = [move for move in history[-8:] if move in MOVES]
    if not recent:
        return None, 0.0
    counts = {move: recent.count(move) for move in MOVES}
    best = max(counts, key=counts.get)
    return best, counts[best] / max(len(recent), 1)


def choose_move(run: dict[str, Any], state: dict[str, Any], settings: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    players = run.get("players") or [{}, {}]
    me = players[0] if players else {}
    enemy = players[1] if len(players) > 1 else {}
    available = [move for move in MOVES if move_stats(me, move)["charges"] > 0]
    if not available:
        available = list(MOVES)

    room = int((run.get("entity") or {}).get("ROOM_NUM_CID") or state.get("room") or 0)
    boss_room = int(settings.get("boss_room") or 16)
    if room < boss_room:
        non_last = [move for move in available if move_stats(me, move)["charges"] > 1]
        if non_last:
            available = non_last

    enemy_moves = enemy_available_moves(enemy)
    dominant = dominant_response(enemy_moves)
    history = list(state.get("enemy_history") or [])
    recent = history[-1] if history else None
    predicted, confidence = predict_enemy(history)
    if predicted not in enemy_moves:
        predicted = None
        confidence = 0.0

    magic_underbuilt = (
        move_stats(me, "scissor")["atk"] + move_stats(me, "scissor")["def"]
        <= max(8, int(max(
            move_stats(me, "rock")["atk"] + move_stats(me, "rock")["def"],
            move_stats(me, "paper")["atk"] + move_stats(me, "paper")["def"],
        ) * 0.55))
    )
    in_danger = effective_hp(me) <= max(move_stats(enemy, move)["atk"] for move in enemy_moves) * 2
    scores: dict[str, float] = {}
    projections: dict[str, dict[str, Any]] = {}

    for move in available:
        ms = move_stats(me, move)
        all_proj = [project_exchange(me, enemy, move, enemy_move) for enemy_move in enemy_moves]
        safe_cov = sum(1 for item in all_proj if item["our_end_eff"] > 0) / max(len(all_proj), 1)
        kill_cov = sum(1 for item in all_proj if item["enemy_end_eff"] <= 0) / max(len(all_proj), 1)
        worst_our = min(item["our_end_eff"] for item in all_proj)
        avg_enemy = sum(item["enemy_end_eff"] for item in all_proj) / max(len(all_proj), 1)
        score = ms["atk"] * 2.2 + ms["def"] * (1.8 if in_danger else 0.8)
        score += safe_cov * (90 if in_danger else 30)
        score += kill_cov * (60 if room >= boss_room else 28)
        score += max(0, effective_hp(enemy) - avg_enemy) * 0.55
        score += worst_our * (0.7 if in_danger else 0.2)
        if dominant == move:
            score += 45
        if recent in MOVES:
            if COUNTERS.get(recent) == move:
                score += 24
            elif COUNTERS.get(move) == recent:
                score -= 24
        if predicted in MOVES:
            if COUNTERS.get(predicted) == move:
                score += 26 * max(confidence, 0.35)
            elif COUNTERS.get(move) == predicted:
                score -= 26 * max(confidence, 0.35)
        if move == "scissor" and magic_underbuilt:
            tactical = (
                (recent in MOVES and COUNTERS.get(recent) == "scissor")
                or (predicted in MOVES and COUNTERS.get(predicted) == "scissor")
                or dominant == "scissor"
            )
            score -= 6 if tactical and in_danger else 50
        scores[move] = score
        projections[move] = {"safe": safe_cov, "kill": kill_cov, "worst_our": worst_our}

    choice = max(scores, key=scores.get)
    safe_moves = [move for move in scores if projections[move]["safe"] >= 1.0]
    if in_danger and safe_moves and projections[choice]["safe"] < 1.0:
        choice = max(safe_moves, key=lambda move: (scores[move], move_stats(me, move)["def"], move_stats(me, move)["atk"]))
    elif in_danger and recent in MOVES:
        counter = COUNTERS.get(recent)
        if counter in scores and projections[counter]["safe"] >= projections[choice]["safe"]:
            if project_exchange(me, enemy, choice, recent)["our_end_eff"] <= 0 < project_exchange(me, enemy, counter, recent)["our_end_eff"]:
                choice = counter

    return choice, {
        "scores": scores,
        "projection": projections.get(choice, {}),
        "recent": recent,
        "predicted": predicted,
        "predicted_confidence": round(confidence, 3),
        "enemy_available": enemy_moves,
        "magic_underbuilt": magic_underbuilt,
    }


def choose_loot(run: dict[str, Any], settings: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    options = run.get("lootOptions") or []
    players = run.get("players") or []
    me = players[0] if players else {}
    hp, hp_max = health(me)
    missing_hp = max(hp_max - hp, 0)
    scores: list[dict[str, Any]] = []
    for index, option in enumerate(options):
        boon = str(option.get("boonTypeString") or "")
        v1 = int(option.get("selectedVal1") or 0)
        v2 = int(option.get("selectedVal2") or 0)
        score = 0.0
        if boon == "AddMaxHealth":
            score = 70 + v1 * 22
        elif boon == "AddMaxArmor":
            score = 52 + v1 * 12
        elif boon == "Heal":
            score = min(v1, missing_hp) * 9 - 8
        elif boon in {"UpgradeRock", "Upgrade Sword"}:
            score = 35 + v1 * 12 + v2 * 18
        elif boon in {"UpgradePaper", "Upgrade Shield"}:
            score = 28 + v1 * 8 + v2 * 12
        elif boon in {"UpgradeScissor", "Upgrade Magic"}:
            score = -80 + v1 * 8 + v2 * 10
        else:
            score = float(v1 + v2)
        scores.append({"index": index, "boon": boon, "v1": v1, "v2": v2, "score": score})
    if not scores:
        return 0, {"scores": []}
    choice = max(scores, key=lambda row: row["score"])
    return int(choice["index"]), {"scores": scores, "choice": choice}


def extract_enemy_move(response: dict[str, Any]) -> str | None:
    for event in ((response.get("data") or {}).get("events") or []):
        if event.get("type") == "use_move" and int(event.get("playerId", -1)) == 1:
            move = str(event.get("value") or "").lower()
            if move in MOVES:
                return move
    players = (((response.get("data") or {}).get("run") or {}).get("players") or [{}, {}])
    if len(players) > 1:
        move = str(players[1].get("lastMove") or "").lower()
        return move if move in MOVES else None
    return None


def infer_turn_result(me: dict[str, Any], enemy: dict[str, Any]) -> str:
    if me.get("thisPlayerWin"):
        return "win"
    if me.get("otherPlayerWin"):
        return "loss"
    if effective_hp(enemy) <= 0 and effective_hp(me) > 0:
        return "win"
    if effective_hp(me) <= 0 and effective_hp(enemy) > 0:
        return "loss"
    return "draw"


def run_entity(payload: dict[str, Any]) -> dict[str, Any]:
    return (payload.get("data") or {}).get("entity") or {}


def run_data(payload: dict[str, Any]) -> dict[str, Any]:
    return (payload.get("data") or {}).get("run") or {}


def energy_thresholds_line(current: int | None, regen_per_hour: int | None) -> str:
    """Return time until next run is possible, e.g. 'next run in 2h30m'."""
    cur = int(current or 0)
    rph = int(regen_per_hour or 0)
    if rph <= 0:
        return ""
    next_t = next((t for t in [40, 80, 120, 160, 200, 240] if t > cur), None)
    if next_t is None:
        return ""
    minutes = int(((next_t - cur) / rph) * 60)
    if minutes < 60:
        label = f"{minutes}m"
    else:
        h, m = divmod(minutes, 60)
        label = f"{h}h{m:02d}m" if m else f"{h}h"
    return f"next run in {label}"


def room_floor(room: int) -> int:
    return max(1, (max(room, 1) - 1) // 4 + 1)


def room_on_floor(room: int) -> int:
    return ((max(room, 1) - 1) % 4) + 1


def format_floor_room(room: int | None) -> str:
    value = int(room or 0)
    if value <= 0:
        return "floor - room=-"
    return f"floor {room_floor(value)} room {room_on_floor(value)}"


def combatant_line(name: str, player: dict[str, Any]) -> str:
    hp, hp_max = health(player)
    sh, sh_max = shield(player)
    moves = " ".join(
        f"{move_label(move)} {move_stats(player, move)['charges']}/3"
        for move in MOVES
    )
    return f"<b>{e(name)}</b> HP {hp}/{hp_max or '-'} ARM {sh}/{sh_max or '-'}\n{e(moves)}"


def account_snapshot(client: GigaverseClient, settings: dict[str, Any]) -> dict[str, Any]:
    me = client.get_user_me()
    address = str(settings.get("player_address") or me.get("address") or "")
    account = client.get_account(address) if address else {}
    energy = client.get_energy(address) if address else {}
    game = me.get("gameAccount") or {}
    account_entity = account.get("accountEntity") or {}
    noob = account.get("noob") or {}
    energy_entity = (energy.get("entities") or [{}])[0]
    parsed_energy = energy_entity.get("parsedData") or {}
    return {
        "address": address,
        "username": account.get("primaryUsername") or game.get("username") or "",
        "noob_id": str(account_entity.get("NOOB_TOKEN_CID") or noob.get("docId") or settings.get("noob_id") or ""),
        "energy": {
            "current": parsed_energy.get("energyValue"),
            "max": parsed_energy.get("maxEnergy"),
            "regen_per_hour": parsed_energy.get("regenPerHour"),
            "is_juiced": bool(parsed_energy.get("isPlayerJuiced")),
            "updated_at": energy_entity.get("updatedAt"),
        },
        "game": {
            "can_enter_game": game.get("canEnterGame"),
            "noob_pass_balance": game.get("noobPassBalance"),
        },
        "gear_instance_ids": list(settings.get("gear_instance_ids") or []),
    }


def format_status(user: dict[str, Any], snapshot: dict[str, Any], dungeon_payload: dict[str, Any] | None = None) -> str:
    settings = deep_merge(DEFAULT_SETTINGS, user.get("settings") or {})
    state = deep_merge(DEFAULT_STATE, user.get("state") or {})
    energy = snapshot.get("energy") or {}
    game = snapshot.get("game") or {}
    run = ((dungeon_payload or {}).get("data") or {}).get("run") or {}
    entity = ((dungeon_payload or {}).get("data") or {}).get("entity") or {}
    try:
        daily = get_daily_run_stats(int(user["telegram_id"]))
    except Exception as exc:  # noqa: BLE001
        LOG.info("Daily stats unavailable: %s", exc)
        daily = {"runs": 0, "completed": 0, "defeated": 0, "best_label": "-", "loot_value": {}}
    last_run = state.get("last_run_summary") or {}
    lines = [
        "<b>Gigaverse Control</b>",
        f"Bot: <b>{'running' if user.get('active') else 'stopped'}</b> | Runs left: {e(state.get('runs_remaining') or 0)}",
        f"Wallet: <code>{e(short_address(snapshot.get('address')))}</code> | Noob <b>{e(snapshot.get('noob_id') or '-')}</b>",
        f"Energy: <b>{e(energy.get('current') or '-')}/{e(energy.get('max') or '-')}</b>"
        + (f" | {e(energy_thresholds_line(energy.get('current'), energy.get('regen_per_hour')))}" if energy_thresholds_line(energy.get('current'), energy.get('regen_per_hour')) else ""),
        f"Dungeon {e(settings.get('dungeon_id'))}",
        "",
        "<b>Last 24h</b>",
        f"Runs: <b>{e(daily.get('runs'))}</b> | completed {e(daily.get('completed'))} | defeated {e(daily.get('defeated'))}",
        f"Best: <b>{e(daily.get('best_label'))}</b> | Loot value {e(format_loot_value(daily.get('loot_value')))}",
    ]
    if last_run:
        loot_value = last_run.get("loot_value") or {}
        lines += [
            "",
            "<b>Last run</b>",
            e(last_run.get("message") or f"Run finished: {last_run.get('status', '-')}")
            + f" | Loot {e(format_loot_value(loot_value))}",
        ]

    if run:
        players = run.get("players") or [{}, {}]
        me = players[0] if players else {}
        enemy = players[1] if len(players) > 1 else {}
        room = int(entity.get("ROOM_NUM_CID") or state.get("room") or 0)
        enemy_name = str(entity.get("ENEMY_CID") or "Enemy")
        lines += [
            "",
            f"<b>Live battle</b> {format_floor_room(room)}",
            combatant_line("You", me),
            combatant_line(enemy_name, enemy),
        ]
    else:
        lines += ["", "<b>Live battle</b>: idle"]
    events = activity_lines(state)
    if events:
        lines += ["", "<b>Events</b>"]
        lines += [e(line) for line in events]
    if state.get("last_error"):
        lines += ["", f"Last error: <code>{e(state.get('last_error'))}</code>"]
    lines.append(f"\nUpdated: {datetime.now().strftime('%m-%d %H:%M:%S')}")
    return "\n".join(lines)


def short_address(address: Any) -> str:
    value = str(address or "")
    if len(value) <= 12:
        return value or "-"
    return f"{value[:6]}...{value[-4:]}"


def append_activity(state: dict[str, Any], text: str) -> dict[str, Any]:
    events = list(state.get("activity_log") or [])
    events.insert(0, {"at": utc_now(), "text": text})
    state["activity_log"] = events[:8]
    return state


def activity_lines(state: dict[str, Any], limit: int = 4) -> list[str]:
    rows: list[str] = []
    for event in list(state.get("activity_log") or [])[:limit]:
        at = str((event or {}).get("at") or "")
        label = str((event or {}).get("text") or "").strip()
        if not label:
            continue
        try:
            stamp = datetime.fromisoformat(at.replace("Z", "+00:00")).strftime("%H:%M")
        except ValueError:
            stamp = "--:--"
        rows.append(f"{stamp} {label}")
    return rows


def _maybe_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def wei_to_eth_decimal(value: int) -> Decimal:
    return Decimal(value) / Decimal(10**18)


def wei_to_eth_str(value: int | None) -> str:
    if value is None:
        return "-"
    amount = wei_to_eth_decimal(int(value))
    if amount == 0:
        return "0"
    return f"{amount:.6f}".rstrip("0").rstrip(".")


def format_loot_value(value: dict[str, Any] | None) -> str:
    data = value or {}
    total_wei = _maybe_int(data.get("total_wei")) or 0
    if total_wei <= 0:
        unpriced = int(data.get("unpriced_items") or 0)
        return "unpriced" if unpriced else "0"
    eth = wei_to_eth_str(total_wei)
    usdc = str(data.get("total_usdc") or "").strip()
    if usdc:
        return f"{eth} ETH / ${usdc}"
    return f"{eth} ETH"


def parse_jsonish(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return default


def normalize_floor_prices(payload: Any) -> dict[int, int]:
    entities = payload.get("entities") if isinstance(payload, dict) else None
    if not isinstance(entities, list):
        return {}
    rows: dict[int, int] = {}
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        item_id = _maybe_int(entity.get("GAME_ITEM_ID_CID"))
        price = _maybe_int(entity.get("ETH_MINT_PRICE_CID"))
        if item_id is not None and price is not None:
            rows[item_id] = price
    return rows


def fetch_eth_usdc_rate() -> Decimal | None:
    configured = _safe_decimal(os.environ.get("ETH_USDC_RATE"))
    if configured and configured > 0:
        return configured
    try:
        response = requests.get("https://api.coinbase.com/v2/prices/ETH-USDC/spot", timeout=8)
        if not response.ok:
            return None
        amount = _safe_decimal(((response.json() or {}).get("data") or {}).get("amount"))
        return amount if amount and amount > 0 else None
    except Exception:  # noqa: BLE001
        return None


def aggregate_drops(drops: list[dict[str, Any]]) -> list[dict[str, Any]]:
    totals: dict[int, dict[str, Any]] = {}
    for drop in drops:
        item_id = _maybe_int(drop.get("item_id") or (drop.get("raw") or {}).get("id"))
        amount = _maybe_int(drop.get("amount")) or 0
        if item_id is None or amount <= 0:
            continue
        row = totals.setdefault(item_id, {"item_id": item_id, "amount": 0, "rarity": drop.get("rarity")})
        row["amount"] += amount
        if row.get("rarity") is None:
            row["rarity"] = drop.get("rarity")
    return sorted(totals.values(), key=lambda row: (-int(row["amount"]), int(row["item_id"])))


def value_run_drops(client: GigaverseClient, drops: list[dict[str, Any]]) -> dict[str, Any]:
    items = aggregate_drops(drops)
    floor_prices: dict[int, int] = {}
    try:
        floor_prices = normalize_floor_prices(client.get_marketplace_floor_all())
    except Exception as exc:  # noqa: BLE001
        LOG.info("Loot value floor fetch failed: %s", exc)
    total_wei = 0
    priced_items = 0
    unpriced_items = 0
    lines: list[str] = []
    for row in items:
        item_id = int(row["item_id"])
        amount = int(row["amount"])
        floor_wei = floor_prices.get(item_id, 0)
        value_wei = floor_wei * amount
        if value_wei > 0:
            total_wei += value_wei
            priced_items += 1
            suffix = f" ({wei_to_eth_str(value_wei)} ETH)"
        else:
            unpriced_items += 1
            suffix = ""
        lines.append(f"Item #{item_id} +{amount}{suffix}")
    rate = fetch_eth_usdc_rate()
    total_usdc = ""
    if rate and total_wei > 0:
        total_usdc = f"{(wei_to_eth_decimal(total_wei) * rate):.2f}"
    return {
        "items": items,
        "lines": lines[:6],
        "total_wei": str(total_wei),
        "total_eth": wei_to_eth_str(total_wei),
        "total_usdc": total_usdc,
        "priced_items": priced_items,
        "unpriced_items": unpriced_items,
    }


def build_run_summary(debug: dict[str, Any], client: GigaverseClient | None = None) -> dict[str, Any]:
    room = int(debug.get("rooms_cleared") or 0)
    status = str(debug.get("status") or "unknown")
    loot_value = {"lines": [], "total_wei": "0", "total_eth": "0", "total_usdc": "", "unpriced_items": 0}
    if client is not None:
        loot_value = value_run_drops(client, list(debug.get("drops") or []))
    return {
        "status": status,
        "rooms_cleared": room,
        "floor": room_floor(room) if room else 0,
        "room_on_floor": room_on_floor(room) if room else 0,
        "message": f"Run finished: {status} {format_floor_room(room)}",
        "loot_value": loot_value,
        "loot_picks": [
            f"R{row.get('room')}:{row.get('boon')}({row.get('v1')},{row.get('v2')})"
            for row in list(debug.get("loot") or [])[-6:]
        ],
    }


def get_daily_run_stats(telegram_id: int) -> dict[str, Any]:
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    rows = sb_get(
        DEBUG_TABLE,
        {
            "telegram_id": f"eq.{telegram_id}",
            "created_at": f"gte.{since}",
            "select": "status,rooms_cleared,loot_value",
        },
    )
    best_room = 0
    completed = 0
    defeated = 0
    total_wei = 0
    total_usdc = 0.0
    for row in rows:
        room = int(row.get("rooms_cleared") or 0)
        best_room = max(best_room, room)
        status = str(row.get("status") or "")
        if status == "completed":
            completed += 1
        elif status:
            defeated += 1
        value = parse_jsonish(row.get("loot_value"), {})
        total_wei += _maybe_int(value.get("total_wei") if isinstance(value, dict) else None) or 0
        try:
            total_usdc += float(value.get("total_usdc") or 0) if isinstance(value, dict) else 0
        except (TypeError, ValueError):
            pass
    usdc_str = f"{total_usdc:.2f}" if total_usdc > 0 else ""
    return {
        "runs": len(rows),
        "completed": completed,
        "defeated": defeated,
        "best_room": best_room,
        "best_label": format_floor_room(best_room) if best_room else "-",
        "loot_value": {"total_wei": str(total_wei), "total_eth": wei_to_eth_str(total_wei), "total_usdc": usdc_str},
    }


def upsert_pinned_status(user: dict[str, Any], text: str) -> dict[str, Any]:
    telegram_id = int(user["telegram_id"])
    state = deep_merge(DEFAULT_STATE, user.get("state") or {})
    mid = state.get("status_msg_id")
    if mid and edit(telegram_id, int(mid), text, reply_markup=main_keyboard()):
        return state
    mid = send(telegram_id, text, reply_markup=main_keyboard(), silent=True)
    if mid:
        pin_message(telegram_id, mid)
        state["status_msg_id"] = mid
        state["last_status_at"] = utc_now()
        update_user(telegram_id, state=state)
    return state


def live_status_for_user(user: dict[str, Any]) -> str:
    settings = deep_merge(DEFAULT_SETTINGS, user.get("settings") or {})
    state = deep_merge(DEFAULT_STATE, user.get("state") or {})
    token = decrypt_secret(stored_bearer_value(user))
    client = GigaverseClient(token, settings, state)
    snapshot = account_snapshot(client, settings)
    dungeon = client.get_dungeon_state()
    return format_status(user, snapshot, dungeon)


def refresh_pinned_for_user(user: dict[str, Any]) -> None:
    telegram_id = int(user["telegram_id"])
    try:
        if stored_bearer_value(user):
            text = live_status_for_user(user)
        else:
            text = format_status(user, {}, None)
        upsert_pinned_status(user, text)
    except Exception as exc:  # noqa: BLE001
        LOG.info("Pinned status refresh failed for %s: %s", telegram_id, exc)


def command_help() -> str:
    return (
        "<b>Gigaverse TG Bot</b>\n\n"
        "/settoken BEARER - save bearer token, message is deleted\n"
        "/token - ask bot to wait for the next bearer token message\n"
        "/setaddress 0x... - optional wallet override\n"
        "/setgear id1,id2 - gear instance ids for run start\n"
        "/setdungeon 1 - dungeon id\n"
        "/setruns 3 - default batch size\n"
        "/run [n] - start n runs through matrix worker\n"
        "/status - refresh pinned account/battle status\n"
        "/stop - stop worker for your user\n"
        "/settings - inline settings menu\n\n"
        "Bearer tokens are stored in Supabase. GIGA_SECRET_KEY is optional for extra encryption."
    )


def handle_start(message: dict[str, Any]) -> None:
    user = ensure_user(message.get("from") or {})
    send(int(user["telegram_id"]), command_help(), reply_markup=main_keyboard())


def prompt_for_token(user: dict[str, Any]) -> None:
    telegram_id = int(user["telegram_id"])
    state = deep_merge(DEFAULT_STATE, user.get("state") or {})
    state["awaiting"] = "bearer_token"
    update_user(telegram_id, state=state)
    send(
        telegram_id,
        "Send your Gigaverse bearer token in the next message.\n\n"
        "You can paste either <code>ey...</code> or <code>Bearer ey...</code>. "
        "I will delete the message and store the token in Supabase.",
        reply_markup=main_keyboard(),
    )


def save_bearer_from_message(message: dict[str, Any], raw_token: str) -> None:
    chat_id = int((message.get("chat") or {}).get("id"))
    message_id = int(message.get("message_id") or 0)
    token = normalize_bearer_token(raw_token)
    if not token:
        send(chat_id, "Token is empty. Send /token and paste it again.")
        return
    if message_id:
        delete_message(chat_id, message_id)
    user = ensure_user(message.get("from") or {})
    state = deep_merge(DEFAULT_STATE, user.get("state") or {})
    state["awaiting"] = ""
    state["last_error"] = ""
    telegram_id = int(user["telegram_id"])
    save_user_secret(telegram_id, encrypt_secret(token))
    update_user(telegram_id, state=state)
    saved_as = "encrypted" if os.environ.get("GIGA_SECRET_KEY", "").strip() else "saved"
    send(chat_id, f"Bearer token {saved_as} in Supabase. Use /status or /run 1.", reply_markup=main_keyboard())


def handle_settoken(message: dict[str, Any], text: str) -> None:
    chat_id = int((message.get("chat") or {}).get("id"))
    parts = text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        user = ensure_user(message.get("from") or {})
        prompt_for_token(user)
        return
    save_bearer_from_message(message, parts[1])


def handle_simple_setting(message: dict[str, Any], text: str) -> None:
    user = ensure_user(message.get("from") or {})
    telegram_id = int(user["telegram_id"])
    settings = deep_merge(DEFAULT_SETTINGS, user.get("settings") or {})
    cmd, _, arg = text.partition(" ")
    arg = arg.strip()
    if cmd == "/setaddress":
        settings["player_address"] = arg
    elif cmd == "/setdungeon":
        settings["dungeon_id"] = int(arg or "1")
    elif cmd == "/setruns":
        settings["runs_to_play"] = max(1, int(arg or "1"))
    elif cmd == "/setgear":
        settings["gear_instance_ids"] = [item.strip() for item in arg.replace(";", ",").split(",") if item.strip()]
    elif cmd == "/setdelay":
        settings["move_delay_sec"] = max(0.1, float(arg or "0.35"))
    else:
        send(telegram_id, "Unknown setting command.")
        return
    state = deep_merge(DEFAULT_STATE, user.get("state") or {})
    state = append_activity(state, "Settings saved")
    update_user(telegram_id, settings=settings, state=state)
    refresh_pinned_for_user(user | {"settings": settings, "state": state})


def request_run(user: dict[str, Any], runs: int) -> None:
    telegram_id = int(user["telegram_id"])
    state = deep_merge(DEFAULT_STATE, user.get("state") or {})
    settings = deep_merge(DEFAULT_SETTINGS, user.get("settings") or {})
    runs = max(1, int(runs or settings.get("runs_to_play") or 1))
    state["command"] = {"action": "start", "runs": runs, "created_at": utc_now()}
    state["runs_remaining"] = runs
    state["last_error"] = ""
    state = append_activity(state, f"Start requested: {runs} run(s)")
    update_user(telegram_id, active=True, state=state)
    dispatch_matrix_for_user(telegram_id)
    refresh_pinned_for_user(user | {"active": True, "state": state})


def handle_run(message: dict[str, Any], text: str) -> None:
    user = ensure_user(message.get("from") or {})
    _, _, arg = text.partition(" ")
    settings = deep_merge(DEFAULT_SETTINGS, user.get("settings") or {})
    request_run(user, int(arg or settings.get("runs_to_play") or 1))


def handle_stop(message: dict[str, Any]) -> None:
    user = ensure_user(message.get("from") or {})
    state = deep_merge(DEFAULT_STATE, user.get("state") or {})
    state["command"] = {"action": "stop", "created_at": utc_now()}
    state = append_activity(state, "Stop requested")
    update_user(int(user["telegram_id"]), active=False, state=state)
    refresh_pinned_for_user(user | {"active": False, "state": state})


def handle_status(message: dict[str, Any]) -> None:
    user = ensure_user(message.get("from") or {})
    chat_id = int(user["telegram_id"])
    if not stored_bearer_value(user):
        send(chat_id, "Token is missing. Use /settoken BEARER.")
        return
    try:
        text = live_status_for_user(user)
        upsert_pinned_status(user, text)
    except Exception as exc:  # noqa: BLE001
        send(chat_id, f"Status failed: <code>{e(exc)}</code>")


def handle_message(update: dict[str, Any]) -> None:
    message = update.get("message") or {}
    text = str(message.get("text") or "").strip()
    if not text:
        return
    user = ensure_user(message.get("from") or {})
    state = deep_merge(DEFAULT_STATE, user.get("state") or {})
    if state.get("awaiting") == "bearer_token" and not text.startswith("/"):
        save_bearer_from_message(message, text)
        return
    if text.startswith("/start") or text.startswith("/help"):
        handle_start(message)
    elif text.startswith("/token") or text.startswith("/settoken") or text.startswith("/setbearer"):
        handle_settoken(message, text)
    elif text.startswith(("/setaddress", "/setdungeon", "/setruns", "/setgear", "/setdelay")):
        handle_simple_setting(message, text)
    elif text.startswith("/run"):
        handle_run(message, text)
    elif text.startswith("/stop"):
        handle_stop(message)
    elif text.startswith("/settings"):
        user = ensure_user(message.get("from") or {})
        send(int(user["telegram_id"]), "Settings", reply_markup=settings_keyboard(deep_merge(DEFAULT_SETTINGS, user.get("settings") or {})))
    elif text.startswith("/status"):
        handle_status(message)
    else:
        user = ensure_user(message.get("from") or {})
        send(int(user["telegram_id"]), "Unknown command. Use /help.", reply_markup=main_keyboard())


def handle_callback(update: dict[str, Any]) -> None:
    callback = update.get("callback_query") or {}
    data = str(callback.get("data") or "")
    message = callback.get("message") or {}
    from_user = callback.get("from") or {}
    user = ensure_user(from_user)
    telegram_id = int(user["telegram_id"])
    settings = deep_merge(DEFAULT_SETTINGS, user.get("settings") or {})
    if data == "home":
        answer_callback(str(callback.get("id")), "OK")
        edit(telegram_id, int(message.get("message_id")), command_help(), reply_markup=main_keyboard())
    elif data == "settings":
        answer_callback(str(callback.get("id")), "Settings")
        edit(telegram_id, int(message.get("message_id")), "Settings", reply_markup=settings_keyboard(settings))
    elif data == "setup:token":
        answer_callback(str(callback.get("id")), "Paste token")
        prompt_for_token(user)
    elif data == "status":
        answer_callback(str(callback.get("id")), "Refreshing")
        handle_status({"from": from_user, "chat": {"id": telegram_id}, "message_id": message.get("message_id")})
    elif data == "stop":
        answer_callback(str(callback.get("id")), "Stopping")
        handle_stop({"from": from_user, "chat": {"id": telegram_id}})
    elif data.startswith("run:"):
        value = data.split(":", 1)[1]
        runs = int(settings.get("runs_to_play") or 1) if value in {"batch", "saved"} else int(value)
        answer_callback(str(callback.get("id")), f"Run {runs}")
        request_run(user, runs)
    elif data == "toggle:auto":
        settings["auto_continue"] = not bool(settings.get("auto_continue"))
        update_user(telegram_id, settings=settings)
        answer_callback(str(callback.get("id")), "Saved")
        edit(telegram_id, int(message.get("message_id")), "Settings", reply_markup=settings_keyboard(settings))
    elif data == "toggle:loot":
        settings["loot_priority"] = "damage" if settings.get("loot_priority") == "health" else "health"
        update_user(telegram_id, settings=settings)
        answer_callback(str(callback.get("id")), "Saved")
        edit(telegram_id, int(message.get("message_id")), "Settings", reply_markup=settings_keyboard(settings))
    else:
        answer_callback(str(callback.get("id")), "Unknown button")


def run_bot(duration: int) -> None:
    end = time.monotonic() + duration
    offset = get_bot_offset()
    LOG.info("Telegram bot polling started at offset %s", offset)
    while time.monotonic() < end:
        try:
            data = tg_get("getUpdates", {"timeout": 25, "offset": offset + 1, "allowed_updates": json.dumps(["message", "callback_query"])})
            for update in data.get("result") or []:
                offset = max(offset, int(update.get("update_id") or 0))
                if update.get("message"):
                    handle_message(update)
                elif update.get("callback_query"):
                    handle_callback(update)
            save_bot_offset(offset)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("Polling error: %s", exc)
            time.sleep(5)


def start_debug(settings: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
    run = run_data(response)
    entity = run_entity(response)
    return {
        "external_run_id": str(run.get("_id") or entity.get("docId") or ""),
        "started_at": utc_now(),
        "status": "running",
        "rooms_cleared": int(entity.get("ROOM_NUM_CID") or 0),
        "wins": 0,
        "losses": 0,
        "draws": 0,
        "loot": [],
        "drops": [],
        "enemy_report": {},
        "combat_log": [],
        "settings_snapshot": settings,
    }


def append_drops(debug: dict[str, Any], response: dict[str, Any], room: int, action: str) -> None:
    for change in response.get("gameItemBalanceChanges") or []:
        item_id = change.get("itemId") or change.get("itemID") or change.get("id")
        debug.setdefault("drops", []).append(
            {
                "at": utc_now(),
                "room": room,
                "action": action,
                "item_id": item_id,
                "amount": change.get("amount") or change.get("delta") or change.get("value"),
                "rarity": change.get("rarity"),
                "gear_instance_id": change.get("gearInstanceId") or "",
                "raw": change,
            }
        )


def tick_worker(user: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    telegram_id = int(user["telegram_id"])
    settings = deep_merge(DEFAULT_SETTINGS, user.get("settings") or {})
    state = deep_merge(DEFAULT_STATE, user.get("state") or {})
    token = decrypt_secret(stored_bearer_value(user))
    if not token:
        state["last_error"] = "Bearer token is missing."
        update_user(telegram_id, state=state, active=False)
        return state, False
    client = GigaverseClient(token, settings, state)

    command = state.get("command") or {}
    if command.get("action") == "stop":
        state["command"] = None
        state["runs_remaining"] = 0
        state["last_error"] = ""
        state = append_activity(state, "Worker stopped")
        update_user(telegram_id, state=state, active=False)
        refresh_pinned_for_user(user | {"state": state, "active": False})
        return state, False
    if command.get("action") == "start":
        state["runs_remaining"] = max(1, int(command.get("runs") or state.get("runs_remaining") or 1))
        state["command"] = None

    account = {}
    dungeon = client.get_dungeon_state()
    data = dungeon.get("data") or {}
    run = data.get("run")
    entity = data.get("entity") or {}

    if not run:
        if state.get("debug"):
            debug = state["debug"]
            debug["ended_at"] = utc_now()
            debug["status"] = "completed" if state.get("last_completion_seen") else "defeated"
            try:
                account = account_snapshot(client, settings)
                debug["account_snapshot"] = account
            except Exception:
                account = {}
            summary = build_run_summary(debug, client)
            debug["loot_value"] = summary.get("loot_value") or {}
            state["last_run_summary"] = summary
            state["last_error"] = ""
            state = append_activity(state, str(summary.get("message") or "Run finished"))
            save_debug_run(telegram_id, debug)
            state["debug"] = None
            state["current_run"] = None
            state["enemy_history"] = []
            state["last_move"] = ""
            state["last_streak"] = 0
            state["last_completion_seen"] = False
            state["runs_remaining"] = max(0, int(state.get("runs_remaining") or 0) - 1)

        should_start = int(state.get("runs_remaining") or 0) > 0 or bool(settings.get("auto_continue"))
        if should_start:
            response = client.start_run()
            if not response.get("success", True):
                raise ApiError(f"start_run failed: {response.get('message') or 'unknown'}")
            state["debug"] = start_debug(settings, response)
            state["current_run"] = {"started_at": utc_now(), "external_run_id": state["debug"].get("external_run_id")}
            run = run_data(response)
            entity = run_entity(response)
            append_drops(state["debug"], response, int(entity.get("ROOM_NUM_CID") or 1), "start_run")
            state = append_activity(state, "Run started")
        else:
            try:
                account = account_snapshot(client, settings)
                state = upsert_pinned_status(user | {"state": state}, format_status(user | {"state": state}, account, dungeon))
            except Exception as exc:  # noqa: BLE001
                state["last_error"] = str(exc)
            keep_active = bool(settings.get("auto_continue") or int(state.get("runs_remaining") or 0) > 0)
            update_user(telegram_id, state=state, active=keep_active)
            return state, keep_active

    if run:
        room = int(entity.get("ROOM_NUM_CID") or state.get("room") or 1)
        state["room"] = room
        players = run.get("players") or [{}, {}]
        me_before = players[0] if players else {}
        enemy_before = players[1] if len(players) > 1 else {}
        if run.get("lootPhase"):
            choice_index, loot_decision = choose_loot(run, settings)
            action = LOOT_ACTIONS[choice_index] if choice_index < len(LOOT_ACTIONS) else "loot_one"
            option = (run.get("lootOptions") or [{}])[choice_index]
            response = client.dungeon_action(action)
            if not response.get("success", True):
                raise ApiError(f"{action} failed: {response.get('message') or 'unknown'}")
            debug = state.get("debug") or start_debug(settings, response)
            debug.setdefault("loot", []).append(
                {
                    "at": utc_now(),
                    "room": room,
                    "choice": choice_index,
                    "boon": option.get("boonTypeString"),
                    "v1": option.get("selectedVal1"),
                    "v2": option.get("selectedVal2"),
                    "decision": loot_decision,
                }
            )
            append_drops(debug, response, room, action)
            state["debug"] = debug
        else:
            move, decision = choose_move(run | {"entity": entity}, state, settings)
            time.sleep(max(0.05, float(settings.get("move_delay_sec") or 0.35)))
            response = client.dungeon_action(move)
            if not response.get("success", True):
                message = str(response.get("message") or "unknown")
                if "cooldown" in message.lower() or "unavailable" in message.lower():
                    cooldowns = list(state.get("move_cooldowns") or [])
                    if move not in cooldowns:
                        cooldowns.append(move)
                    state["move_cooldowns"] = cooldowns
                    update_user(telegram_id, state=state)
                    return state, True
                raise ApiError(f"{move} failed: {message}")
            next_run = run_data(response)
            next_entity = run_entity(response)
            next_players = next_run.get("players") or [{}, {}]
            me_after = next_players[0] if next_players else {}
            enemy_after = next_players[1] if len(next_players) > 1 else {}
            enemy_move = extract_enemy_move(response)
            result = infer_turn_result(me_after, enemy_after)
            history = list(state.get("enemy_history") or [])
            if enemy_move in MOVES:
                history.append(enemy_move)
            state["enemy_history"] = history[-80:]
            state["move_cooldowns"] = []
            if state.get("last_move") == move:
                state["last_streak"] = int(state.get("last_streak") or 0) + 1
            else:
                state["last_move"] = move
                state["last_streak"] = 1
            debug = state.get("debug") or start_debug(settings, response)
            debug["rooms_cleared"] = max(int(debug.get("rooms_cleared") or 0), int(next_entity.get("ROOM_NUM_CID") or room))
            if result == "win":
                debug["wins"] = int(debug.get("wins") or 0) + 1
            elif result == "loss":
                debug["losses"] = int(debug.get("losses") or 0) + 1
            else:
                debug["draws"] = int(debug.get("draws") or 0) + 1
            enemy_key = str(entity.get("ENEMY_CID") or "unknown")
            report = debug.setdefault("enemy_report", {})
            enemy_row = report.setdefault(enemy_key, {"turns": 0, "moves": {}, "wins": 0, "losses": 0, "draws": 0})
            enemy_row["turns"] += 1
            enemy_row["moves"][enemy_move or "?"] = enemy_row["moves"].get(enemy_move or "?", 0) + 1
            result_key = {"win": "wins", "loss": "losses", "draw": "draws"}.get(result, "draws")
            enemy_row[result_key] = enemy_row.get(result_key, 0) + 1
            debug.setdefault("combat_log", []).append(
                {
                    "at": utc_now(),
                    "room": room,
                    "floor": room_floor(room),
                    "enemy_id": enemy_key,
                    "our_move": move,
                    "enemy_move": enemy_move,
                    "result": result,
                    "before": {
                        "our_hp": health(me_before)[0],
                        "our_shield": shield(me_before)[0],
                        "enemy_hp": health(enemy_before)[0],
                        "enemy_shield": shield(enemy_before)[0],
                    },
                    "after": {
                        "our_hp": health(me_after)[0],
                        "our_shield": shield(me_after)[0],
                        "enemy_hp": health(enemy_after)[0],
                        "enemy_shield": shield(enemy_after)[0],
                    },
                    "decision": decision,
                }
            )
            append_drops(debug, response, room, move)
            state["debug"] = debug
            if next_entity.get("COMPLETE_CID"):
                state["last_completion_seen"] = True

        try:
            account = account_snapshot(client, settings)
            latest_dungeon = client.get_dungeon_state()
            state = upsert_pinned_status(user | {"state": state}, format_status(user | {"state": state}, account, latest_dungeon))
        except Exception as exc:  # noqa: BLE001
            state["last_error"] = str(exc)
    state["last_error"] = ""
    update_user(telegram_id, state=state, active=bool(user.get("active")))
    return state, True


def run_worker(telegram_id: int, duration: int, interval: float) -> int:
    end = time.monotonic() + duration
    LOG.info("Worker started for %s", telegram_id)
    exit_code = 0
    while time.monotonic() < end:
        user = get_user(telegram_id)
        if not user:
            LOG.info("User %s not found", telegram_id)
            return 0
        if not user.get("active") and not ((user.get("state") or {}).get("command") or {}).get("action") == "stop":
            LOG.info("User %s inactive", telegram_id)
            return 0
        try:
            _, keep_running = tick_worker(user)
            if not keep_running:
                return exit_code
        except ApiError as exc:
            LOG.warning("[%s] API error: %s", telegram_id, exc)
            if exc.status_code == 429:
                exit_code = 2
                break
            state = deep_merge(DEFAULT_STATE, (user.get("state") or {}))
            state["last_error"] = str(exc)
            update_user(telegram_id, state=state)
            time.sleep(min(interval * 3, 30))
        except Exception as exc:  # noqa: BLE001
            LOG.exception("[%s] Worker error", telegram_id)
            state = deep_merge(DEFAULT_STATE, (user.get("state") or {}))
            state["last_error"] = str(exc)
            update_user(telegram_id, state=state)
            time.sleep(min(interval * 3, 30))
        time.sleep(max(0.5, interval))
    return exit_code


def matrix_output(single_user: str = "") -> None:
    users = [single_user] if single_user else [str(row["telegram_id"]) for row in list_active_users()]
    users = [user for user in users if user]
    matrix = {"telegram_id": users}
    output_path = os.environ.get("GITHUB_OUTPUT")
    lines = [
        f"matrix={compact_json(matrix)}",
        f"has_users={'true' if users else 'false'}",
    ]
    if output_path:
        with open(output_path, "a", encoding="utf-8") as handle:
            for line in lines:
                handle.write(line + "\n")
    for line in lines:
        print(line)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    bot = sub.add_parser("bot")
    bot.add_argument("--duration", type=int, default=20700)
    worker = sub.add_parser("worker")
    worker.add_argument("--user", type=int, required=True)
    worker.add_argument("--duration", type=int, default=20700)
    worker.add_argument("--interval", type=float, default=2.0)
    matrix = sub.add_parser("matrix")
    matrix.add_argument("--single-user", default="")
    return parser


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    args = build_parser().parse_args()
    if args.cmd == "bot":
        run_bot(args.duration)
        return 0
    if args.cmd == "worker":
        return run_worker(args.user, args.duration, args.interval)
    if args.cmd == "matrix":
        matrix_output(args.single_user)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
