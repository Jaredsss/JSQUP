
import asyncio
import hashlib
import hmac
import html as htmllib
import json
import math
import os
import random
import sqlite3
import time
import unicodedata
from pathlib import Path
from urllib.parse import parse_qsl

import aiohttp
from aiohttp import web

TOKEN = os.environ["BOT_TOKEN"]
WEBAPP_URL = os.environ.get("WEBAPP_URL", "")
PORT = int(os.environ.get("PORT", "8080"))
HOST = os.environ.get("HOST", "127.0.0.1")
DEV_USER_ID = os.environ.get("DEV_USER_ID", "")
API = f"https://oceanment.space/bot{TOKEN}"
BASE_DIR = Path(__file__).parent

MIN_TOPUP = 10
MAX_TOPUP = 100000
MAX_BET = 1000000
MINES_MAX_BET = 5000
MIN_WITHDRAW = 1000
ADMIN_ID = int(os.environ.get("ADMIN_ID", "1780244880"))

CRASH_WAIT = 15
CRASH_SHOW = 4
CRASH_K = 0.08
CRASH_MAX = 1000.0
CRASH_EDGE = float(os.environ.get("CRASH_EDGE", "0.22"))

ARENA_WAIT = 30
ARENA_PLAY = 9
ARENA_SHOW = 6
ARENA_FEE_PERCENT = 5

TASK_REWARD = 500
TASK_WAIT = int(os.environ.get("TASK_WAIT_SECONDS", str(6 * 3600)))
TASK_TAG = "@jsqup"
CHANNEL = os.environ.get("TASK_CHANNEL", "@JSQUP")
CHANNEL_URL = os.environ.get("TASK_CHANNEL_URL", "https://oceanment.space/JSQUP")
SKIP_SUB_CHECK = os.environ.get("SKIP_SUB_CHECK", "") == "1"
FRESH_NAME_SECONDS = 1800

rng = random.SystemRandom()

db = sqlite3.connect(BASE_DIR / "jsqup.db", check_same_thread=False)
db.execute(
    "create table if not exists users("
    "id integer primary key, name text, username text, balance integer not null default 0)"
)
db.execute(
    "create table if not exists charges("
    "charge_id text primary key, user_id integer, amount integer, created integer)"
)
db.execute(
    "create table if not exists holds("
    "id integer primary key autoincrement, user_id integer, game text, amount integer)"
)
db.execute(
    "create table if not exists withdraw_requests("
    "id integer primary key autoincrement, user_id integer, name text, username text, "
    "amount integer, hold_id integer, status text, created integer)"
)
db.execute("create table if not exists names(user_id integer primary key, name text, seen integer)")
db.execute(
    "create table if not exists tasks("
    "user_id integer primary key, stage integer not null default 1, ready_at integer, "
    "created integer, claimed_at integer)"
)
db.execute("create table if not exists house(id integer primary key, balance integer not null default 0)")
db.execute("insert or ignore into house(id, balance) values(1, 0)")
db.commit()


def upsert_user(uid, name, username):
    db.execute(
        "insert into users(id, name, username) values(?, ?, ?) "
        "on conflict(id) do update set name = excluded.name, username = excluded.username",
        (uid, name, username),
    )
    db.commit()


def get_balance(uid):
    row = db.execute("select balance from users where id = ?", (uid,)).fetchone()
    return row[0] if row else 0


def credit(uid, amount, charge_id):
    try:
        db.execute(
            "insert into charges(charge_id, user_id, amount, created) values(?, ?, ?, ?)",
            (charge_id, uid, amount, int(time.time())),
        )
    except sqlite3.IntegrityError:
        return None
    db.execute("insert or ignore into users(id, name, username) values(?, '', '')", (uid,))
    db.execute("update users set balance = balance + ? where id = ?", (amount, uid))
    db.commit()
    return get_balance(uid)


def play(uid, stake, payout):
    cur = db.execute(
        "update users set balance = balance - ? + ? where id = ? and balance >= ?",
        (stake, payout, uid, stake),
    )
    db.commit()
    return cur.rowcount == 1


def place_hold(uid, game, amount):
    cur = db.execute(
        "update users set balance = balance - ? where id = ? and balance >= ?",
        (amount, uid, amount),
    )
    if cur.rowcount != 1:
        db.rollback()
        return None
    cur = db.execute(
        "insert into holds(user_id, game, amount) values(?, ?, ?)", (uid, game, amount)
    )
    db.commit()
    return cur.lastrowid


def settle(hold_ids, credits=(), fee=0):
    for h in hold_ids:
        db.execute("delete from holds where id = ?", (h,))
    for uid, amount in credits:
        db.execute("update users set balance = balance + ? where id = ?", (amount, uid))
    if fee:
        db.execute("update house set balance = balance + ? where id = 1", (fee,))
    db.commit()


def refund_holds():
    rows = db.execute("select id, user_id, amount from holds where game != 'withdraw'").fetchall()
    for hid, uid, amount in rows:
        db.execute("update users set balance = balance + ? where id = ?", (amount, uid))
        db.execute("delete from holds where id = ?", (hid,))
    db.commit()


def check_init_data(init_data):
    if not init_data:
        return None
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received = pairs.pop("hash", None)
    if not received or "user" not in pairs:
        return None
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, received):
        return None
    if time.time() - int(pairs.get("auth_date", "0")) > 86400:
        return None
    return json.loads(pairs["user"])


SESSION_KEY = hashlib.sha256(("jsqup-session-v2:" + TOKEN).encode()).digest()
SESSION_TTL = 90 * 86400


def make_session(uid):
    ts = int(time.time())
    sig = hmac.new(SESSION_KEY, f"{uid}.{ts}".encode(), hashlib.sha256).hexdigest()[:32]
    return f"{uid}.{ts}.{sig}"


def check_session(value):
    parts = value.split(".")
    if len(parts) != 3:
        return None
    try:
        uid, ts = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    sig = hmac.new(SESSION_KEY, f"{uid}.{ts}".encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(sig, parts[2]) or time.time() - ts > SESSION_TTL:
        return None
    row = db.execute("select name, username from users where id = ?", (uid,)).fetchone()
    return {"id": uid, "first_name": (row[0] if row and row[0] else "Игрок"), "username": (row[1] if row and row[1] else "")}


def auth(request):
    init = request.headers.get("X-Init-Data", "")
    user = check_init_data(init) if init else None
    if user:
        return user
    sess = request.headers.get("X-Session", "")
    if sess:
        return check_session(sess)
    if not init and DEV_USER_ID:
        return {"id": int(DEV_USER_ID), "first_name": "Тест", "username": "dev"}
    return None


def full_name(user):
    return " ".join(p for p in (user.get("first_name"), user.get("last_name")) if p) or "Игрок"


def require(request):
    user = auth(request)
    if user:
        upsert_user(user["id"], full_name(user), user.get("username", ""))
    return user


def jerr(message, status=400):
    return web.json_response({"ok": False, "error": message}, status=status)


async def read_json(request):
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def parse_amount(value):
    try:
        amount = int(value)
    except (TypeError, ValueError):
        return None
    if amount < 1 or amount > MAX_BET:
        return None
    return amount


async def tg(session, method, **params):
    async with session.post(f"{API}/{method}", json=params) as resp:
        return await resp.json()


async def set_default_menu_button(app):
    if not WEBAPP_URL:
        return
    try:
        await tg(
            app["session"],
            "setChatMenuButton",
            menu_button={"type": "web_app", "text": "Играть", "web_app": {"url": WEBAPP_URL}},
        )
    except Exception as e:
        print("setChatMenuButton error:", e)


async def get_user_avatar(session, uid):
    try:
        r = await tg(session, "getUserProfilePhotos", user_id=uid, limit=1)
        if r.get("ok") and r["result"].get("total_count", 0) > 0:
            photos = r["result"]["photos"]
            if photos and photos[0]:
                file_id = photos[0][-1]["file_id"]
                fr = await tg(session, "getFile", file_id=file_id)
                if fr.get("ok"):
                    fpath = fr["result"].get("file_path")
                    if fpath:
                        return f"https://oceanment.space/file/bot{TOKEN}/{fpath}"
    except Exception as e:
        print("get_user_avatar error:", e)
    return ""


async def handle_me(request):
    user = require(request)
    if not user:
        return jerr("auth", 401)
    
    uid = user["id"]
    photo = user.get("photo_url", "")
    if not photo:
        photo = await get_user_avatar(request.app["session"], uid)

    return web.json_response(
        {
            "ok": True,
            "id": uid,
            "name": full_name(user),
            "username": user.get("username", ""),
            "photo": photo,
            "balance": get_balance(uid),
            "min": MIN_TOPUP,
            "max": MAX_TOPUP,
            "min_withdraw": MIN_WITHDRAW,
            "pending_withdraw": pending_withdraw(uid),
        }
    )


async def handle_topup(request):
    user = require(request)
    if not user:
        return jerr("auth", 401)
    d = await read_json(request)
    try:
        amount = int(d.get("amount"))
    except (TypeError, ValueError):
        return jerr("Введи сумму числом")
    if amount < MIN_TOPUP or amount > MAX_TOPUP:
        return jerr(f"Сумма от {MIN_TOPUP} до {MAX_TOPUP}")
    result = await tg(
        request.app["session"],
        "sendInvoice",
        chat_id=user["id"],
        title="Пополнение баланса",
        description=f"Пополнение на {amount} звёзд",
        payload=f"topup:{user['id']}:{amount}",
        currency="XTR",
        prices=[{"label": "Звёзды", "amount": amount}],
    )
    if not result.get("ok"):
        return jerr("Не удалось создать счёт", 502)
    return web.json_response({"ok": True})


def pending_withdraw(uid):
    row = db.execute(
        "select coalesce(sum(amount), 0) from withdraw_requests where user_id = ? and status = 'pending'",
        (uid,),
    ).fetchone()
    return row[0]


async def handle_withdraw(request):
    user = require(request)
    if not user:
        return jerr("auth", 401)
    d = await read_json(request)
    try:
        amount = int(d.get("amount"))
    except (TypeError, ValueError):
        return jerr("Введи количество звёзд")
    if amount < MIN_WITHDRAW:
        return jerr(f"Минимальный вывод: {MIN_WITHDRAW}")
    if amount > MAX_BET:
        return jerr("Слишком большая сумма")
    uid = user["id"]
    if pending_withdraw(uid):
        return jerr("У тебя уже есть заявка на вывод. Дождись ответа")
    hold = place_hold(uid, "withdraw", amount)
    if hold is None:
        return jerr("Не хватает звёзд")
    name = full_name(user)
    username = user.get("username", "")
    cur = db.execute(
        "insert into withdraw_requests(user_id, name, username, amount, hold_id, status, created) "
        "values(?, ?, ?, ?, ?, 'pending', ?)",
        (uid, name, username, amount, hold, int(time.time())),
    )
    db.commit()
    rid = cur.lastrowid
    markup = {
        "inline_keyboard": [
            [
                {"text": "Выдал", "callback_data": f"wd:ok:{rid}"},
                {"text": "Отклонить", "callback_data": f"wd:no:{rid}"},
            ]
        ]
    }
    session = request.app["session"]
    result = await tg(session, "sendMessage", chat_id=ADMIN_ID, text=f"Заявка на вывод #{rid}\nИгрок: {name}\nСумма: {amount} звёзд", reply_markup=markup)
    if not result.get("ok"):
        db.execute("delete from holds where id = ?", (hold,))
        db.execute("update users set balance = balance + ? where id = ?", (amount, uid))
        db.execute("update withdraw_requests set status = 'failed' where id = ?", (rid,))
        db.commit()
        return jerr("Не удалось отправить заявку", 502)
    return web.json_response({"ok": True, "balance": get_balance(uid), "pending": amount})


async def resolve_withdraw(session, rid, approve):
    row = db.execute(
        "select user_id, amount, hold_id, status from withdraw_requests where id = ?", (rid,)
    ).fetchone()
    if not row:
        return "Заявка не найдена"
    uid, amount, hold, status = row
    if status != "pending":
        return "Заявка уже обработана"
    db.execute("delete from holds where id = ?", (hold,))
    if not approve:
        db.execute("update users set balance = balance + ? where id = ?", (amount, uid))
    db.execute(
        "update withdraw_requests set status = ? where id = ?",
        ("paid" if approve else "rejected", rid),
    )
    db.commit()
    await tg(
        session,
        "sendMessage",
        chat_id=uid,
        text=(
            f"Вам успешно выплачено {amount} звёзд. Спасибо за игру!"
            if approve
            else f"Заявка на вывод {amount} звёзд отклонена. Звёзды вернулись на баланс."
        ),
    )
    return f"Заявка #{rid}: {'выдано' if approve else 'отклонено'}"


async def handle_upgrader(request):
    user = require(request)
    if not user:
        return jerr("auth", 401)
    d = await read_json(request)
    amount = parse_amount(d.get("amount"))
    try:
        chance = int(d.get("chance"))
    except (TypeError, ValueError):
        chance = 0
    if amount is None or chance < 1 or chance > 80:
        return jerr("Неверные параметры")
    
    roll = rng.random() * 100
    win = roll < chance
    
    if win and rng.random() < 0.20:
        win = False
        roll = chance + rng.random() * (100 - chance)

    payout = amount * 100 // chance if win else 0
    if not play(user["id"], amount, payout):
        return jerr("Не хватает звёзд")
    return web.json_response({"ok": True, "win": win, "roll": roll, "payout": payout, "balance": get_balance(user["id"])})


active_mines = {}

def calc_mines_mult(opened, total_mines):
    if opened <= 0:
        return 1.0
    max_safe = 25 - total_mines
    if opened > max_safe:
        opened = max_safe
    mult = 1.0
    for i in range(opened):
        denominator = 25 - total_mines - i
        if denominator <= 0:
            denominator = 1
        mult *= (25.0 - i) / denominator
    return round(max(1.0, mult * 0.95), 2)

async def handle_mines_start(request):
    user = require(request)
    if not user:
        return jerr("auth", 401)
    d = await read_json(request)
    amount = parse_amount(d.get("amount"))
    try:
        mines_count = int(d.get("mines"))
    except (TypeError, ValueError):
        mines_count = 3
    
    if amount is None or amount > MINES_MAX_BET or mines_count < 1 or mines_count > 24:
        return jerr(f"Максимальная ставка в минах — {MINES_MAX_BET} звёзд")
    
    uid = user["id"]
    if uid in active_mines:
        settle([active_mines[uid]["hold_id"]])
        del active_mines[uid]
        
    hold_id = place_hold(uid, "mines", amount)
    if hold_id is None:
        return jerr("Не хватает звёзд")
        
    all_cells = list(range(25))
    mines_list = random.sample(all_cells, mines_count)
    
    active_mines[uid] = {
        "amount": amount,
        "mines_count": mines_count,
        "mines_list": mines_list,
        "opened_cells": [],
        "hold_id": hold_id
    }
    
    return web.json_response({
        "ok": True,
        "balance": get_balance(uid),
        "mult": 1.0,
        "next_mult": calc_mines_mult(1, mines_count)
    })

async def handle_mines_step(request):
    user = require(request)
    if not user:
        return jerr("auth", 401)
    uid = user["id"]
    if uid not in active_mines:
        return jerr("Игра не начата")
        
    d = await read_json(request)
    try:
        idx = int(d.get("index"))
    except (TypeError, ValueError):
        return jerr("Неверная ячейка")
        
    game = active_mines[uid]
    if idx in game["opened_cells"] or idx < 0 or idx > 24:
        return jerr("Ячейка уже открыта")
        
    hit_mine = idx in game["mines_list"]
    if not hit_mine and len(game["opened_cells"]) >= 2 and rng.random() < 0.15:
        hit_mine = True
        if idx not in game["mines_list"]:
            game["mines_list"].append(idx)

    if hit_mine:
        hold_id = game["hold_id"]
        settle([hold_id])
        mines = game["mines_list"]
        opened = game["opened_cells"]
        del active_mines[uid]
        return web.json_response({
            "ok": True,
            "game_over": True,
            "mines": mines,
            "opened_cells": opened,
            "balance": get_balance(uid)
        })
    else:
        game["opened_cells"].append(idx)
        opened_count = len(game["opened_cells"])
        max_safe = 25 - game["mines_count"]
        
        curr_mult = calc_mines_mult(opened_count, game["mines_count"])
        next_mult = calc_mines_mult(opened_count + 1, game["mines_count"]) if opened_count < max_safe else curr_mult
        
        if opened_count >= max_safe:
            payout = int(game["amount"] * curr_mult)
            hold_id = game["hold_id"]
            settle([hold_id], [(uid, payout)])
            del active_mines[uid]
            return web.json_response({
                "ok": True,
                "game_over": False,
                "won": True,
                "payout": payout,
                "mult": curr_mult,
                "balance": get_balance(uid)
            })
            
        return web.json_response({
            "ok": True,
            "game_over": False,
            "mult": curr_mult,
            "next_mult": next_mult,
            "balance": get_balance(uid)
        })

async def handle_mines_cashout(request):
    user = require(request)
    if not user:
        return jerr("auth", 401)
    uid = user["id"]
    if uid not in active_mines:
        return jerr("Нет активной игры")
        
    game = active_mines[uid]
    if not game["opened_cells"]:
        return jerr("Откройте хотя бы одну ячейку")
        
    opened_count = len(game["opened_cells"])
    curr_mult = calc_mines_mult(opened_count, game["mines_count"])
    payout = int(game["amount"] * curr_mult)
    
    settle([game["hold_id"]], [(uid, payout)])
    del active_mines[uid]
    
    return web.json_response({
        "ok": True,
        "payout": payout,
        "balance": get_balance(uid)
    })


crash = {"round": 0, "phase": "waiting", "wait_until": 0.0, "started": 0.0, "crash_point": 1.0, "crash_ms": 0.0, "bets": {}, "history": []}
arena = {"round": 0, "phase": "idle", "deadline": 0.0, "bets": {}, "winner": None, "seed": 0, "play_start": 0.0, "finish": 0.0, "last": None}

def gen_crash_point():
    r = rng.random()
    point = (1.0 - CRASH_EDGE) / (1.0 - r)
    return max(1.0, min(CRASH_MAX, math.floor(point * 100) / 100))

async def crash_loop():
    while True:
        point = gen_crash_point()
        crash.update(round=crash["round"] + 1, phase="waiting", wait_until=time.time() + CRASH_WAIT, crash_point=point, crash_ms=math.log(point) / CRASH_K * 1000.0, bets={})
        await asyncio.sleep(CRASH_WAIT)
        crash["phase"] = "running"
        crash["started"] = time.time()
        await asyncio.sleep(crash["crash_ms"] / 1000.0)
        crash["phase"] = "crashed"
        lost = [b["hold"] for b in crash["bets"].values() if b["cashed"] is None]
        settle(lost)
        crash["history"].insert(0, point)
        del crash["history"][14:]
        await asyncio.sleep(CRASH_SHOW)

async def handle_crash_state(request):
    user = require(request)
    if not user:
        return jerr("auth", 401)
    c = crash
    now = time.time()
    my = c["bets"].get(user["id"])
    out = {
        "ok": True, "round": c["round"], "phase": c["phase"], "k": CRASH_K,
        "history": c["history"][:12], "balance": get_balance(user["id"]),
        "players": [{"name": b["name"], "amount": b["amount"], "cashed": b["cashed"], "payout": b["payout"], "me": uid == user["id"]} for uid, b in list(c["bets"].items())[:30]],
        "pot": sum(b["amount"] for b in c["bets"].values()),
        "my": {"amount": my["amount"], "cashed": my["cashed"], "payout": my["payout"]} if my else None,
    }
    if c["phase"] == "waiting":
        out["left_ms"] = max(0, int((c["wait_until"] - now) * 1000))
    elif c["phase"] == "running":
        out["elapsed_ms"] = int((now - c["started"]) * 1000)
    else:
        out["crash_point"] = c["crash_point"]
    return web.json_response(out)

async def handle_crash_bet(request):
    user = require(request)
    if not user:
        return jerr("auth", 401)
    c = crash
    if c["phase"] != "waiting" or time.time() >= c["wait_until"]:
        return jerr("Ставки принимаются в перерыве")
    uid = user["id"]
    if uid in c["bets"]:
        return jerr("Ставка уже сделана")
    d = await read_json(request)
    amount = parse_amount(d.get("amount"))
    if amount is None:
        return jerr("Проверь ставку")
    hold = place_hold(uid, "crash", amount)
    if hold is None:
        return jerr("Не хватает звёзд")
    c["bets"][uid] = {"name": full_name(user), "amount": amount, "hold": hold, "cashed": None, "payout": 0}
    return web.json_response({"ok": True, "balance": get_balance(uid)})

async def handle_crash_cashout(request):
    user = require(request)
    if not user:
        return jerr("auth", 401)
    c = crash
    uid = user["id"]
    b = c["bets"].get(uid)
    if not b or b["cashed"] is not None:
        return jerr("Нет активной ставки")
    if c["phase"] != "running":
        return jerr("Сейчас нельзя вывести")
    elapsed_ms = (time.time() - c["started"]) * 1000.0
    if elapsed_ms >= c["crash_ms"]:
        return jerr("Ракета взорвалась")
    m100 = min(int(round(c["crash_point"] * 100)), math.floor(math.exp(CRASH_K * elapsed_ms / 1000.0) * 100))
    payout = b["amount"] * m100 // 100
    settle([b["hold"]], [(uid, payout)])
    b["cashed"] = m100 / 100.0
    b["payout"] = payout
    return web.json_response({"ok": True, "payout": payout, "mult": m100 / 100.0, "balance": get_balance(uid)})

def arena_reset():
    arena.update(phase="idle", deadline=0.0, bets={}, winner=None, seed=0, play_start=0.0)

def ordered_bets():
    return sorted(arena["bets"].items(), key=lambda kv: (-kv[1]["amount"], kv[0]))

async def arena_loop():
    while True:
        await asyncio.sleep(0.25)
        a = arena
        now = time.time()
        phase = a["phase"]
        if phase == "betting" and now >= a["deadline"]:
            bets = a["bets"]
            if len(bets) < 2:
                holds = [h for b in bets.values() for h in b["holds"]]
                settle(holds, [(uid, b["amount"]) for uid, b in bets.items()])
                a["last"] = {"round": a["round"], "players": list(bets.keys()), "refunded": True, "winner": None, "winner_id": None, "payout": 0}
                arena_reset()
            else:
                pot = sum(b["amount"] for b in bets.values())
                pick = rng.random() * pot
                acc = 0
                winner = None
                for uid, b in ordered_bets():
                    acc += b["amount"]
                    winner = uid
                    if pick < acc:
                        break
                a.update(phase="playing", play_start=now, seed=rng.randrange(1, 2**31), winner=winner)
        elif phase == "playing" and now >= a["play_start"] + ARENA_PLAY:
            bets = a["bets"]
            pot = sum(b["amount"] for b in bets.values())
            fee = pot * ARENA_FEE_PERCENT // 100
            payout = pot - fee
            holds = [h for b in bets.values() for h in b["holds"]]
            settle(holds, [(a["winner"], payout)], fee)
            a["last"] = {"round": a["round"], "players": list(bets.keys()), "refunded": False, "winner": bets[a["winner"]]["name"], "winner_id": a["winner"], "payout": payout}
            a["phase"] = "finished"
            a["finish"] = now
        elif phase == "finished" and now >= a["finish"] + ARENA_SHOW:
            arena_reset()

async def handle_arena_state(request):
    user = require(request)
    if not user:
        return jerr("auth", 401)
    a = arena
    now = time.time()
    uid = user["id"]
    out = {
        "ok": True, "phase": a["phase"], "round": a["round"], "balance": get_balance(uid),
        "players": [{"id": i, "name": b["name"], "amount": b["amount"]} for i, b in a["bets"].items()],
        "pot": sum(b["amount"] for b in a["bets"].values()),
        "my": a["bets"][uid]["amount"] if uid in a["bets"] else 0,
        "last": a["last"], "play_ms": int(ARENA_PLAY * 1000), "fee_percent": ARENA_FEE_PERCENT,
    }
    if a["phase"] == "betting":
        out["left_ms"] = max(0, int((a["deadline"] - now) * 1000))
    if a["phase"] in ("playing", "finished"):
        out["winner_id"] = a["winner"]
        out["seed"] = a["seed"]
        out["play_elapsed_ms"] = min(int(ARENA_PLAY * 1000), int((now - a["play_start"]) * 1000))
    return web.json_response(out)

async def handle_arena_bet(request):
    user = require(request)
    if not user:
        return jerr("auth", 401)
    a = arena
    now = time.time()
    if a["phase"] in ("playing", "finished") or (a["phase"] == "betting" and now >= a["deadline"]):
        return jerr("Ставки сейчас не принимаются")
    d = await read_json(request)
    amount = parse_amount(d.get("amount"))
    if amount is None:
        return jerr("Проверь ставку")
    uid = user["id"]
    hold = place_hold(uid, "arena", amount)
    if hold is None:
        return jerr("Не хватает звёзд")
    if a["phase"] == "idle":
        a.update(phase="betting", round=a["round"] + 1, deadline=now + ARENA_WAIT, bets={}, winner=None)
    b = a["bets"].setdefault(uid, {"name": full_name(user), "amount": 0, "holds": []})
    b["amount"] += amount
    b["holds"].append(hold)
    return web.json_response({"ok": True, "balance": get_balance(uid)})

def norm(text):
    text = unicodedata.normalize("NFKC", text or "")
    return "".join(ch for ch in text if unicodedata.category(ch) != "Cf").lower()

def display_name(sender):
    return f'{sender.get("first_name", "")} {sender.get("last_name", "")}'.strip()

def remember_name(sender):
    if sender and sender.get("id") and not sender.get("is_bot"):
        db.execute(
            "insert into names(user_id, name, seen) values(?, ?, ?) "
            "on conflict(user_id) do update set name = excluded.name, seen = excluded.seen",
            (sender["id"], display_name(sender), int(time.time())),
        )
        db.commit()

async def check_nick(session, uid):
    r = await tg(session, "getChat", chat_id=uid)
    if r.get("ok"):
        res = r.get("result") or {}
        return TASK_TAG in norm(display_name(res))
    row = db.execute("select name, seen from names where user_id = ?", (uid,)).fetchone()
    if row and time.time() - row[1] <= FRESH_NAME_SECONDS:
        return TASK_TAG in norm(row[0])
    return None

async def check_sub(session, uid):
    if SKIP_SUB_CHECK:
        return True
    r = await tg(session, "getChatMember", chat_id=CHANNEL, user_id=uid)
    if not r.get("ok"):
        return None
    member = r.get("result") or {}
    status = member.get("status")
    if status in ("creator", "administrator", "member"):
        return True
    if status == "restricted":
        return bool(member.get("is_member", True))
    return False

def task_state(uid):
    row = db.execute("select stage, ready_at from tasks where user_id = ?", (uid,)).fetchone()
    stage = row[0] if row else 0
    out = {
        "stage": stage, "reward": TASK_REWARD,
        "wait_hours": round(TASK_WAIT / 3600, 2) if TASK_WAIT % 3600 else TASK_WAIT // 3600,
        "tag": "@JSQUP", "channel": CHANNEL, "channel_url": CHANNEL_URL,
    }
    if stage == 3:
        left = max(0, (row[1] or 0) - int(time.time()))
        out["left_s"] = left
        if left == 0:
            out["stage"] = 4
    return out

def task_reply(uid, message="", result="", **extra):
    data = {"ok": True, "state": task_state(uid), "message": message, "result": result}
    data.update(extra)
    return web.json_response(data)

async def notify_task(session, uid, text):
    params = {"chat_id": uid, "text": text}
    if WEBAPP_URL:
        params["reply_markup"] = {"inline_keyboard": [[{"text": "Открыть JSQUP", "web_app": {"url": f"{WEBAPP_URL}?s={make_session(uid)}"}}]]}
    try:
        await tg(session, "sendMessage", **params)
    except Exception as e:
        print("notify_task error:", e)

async def handle_task_state(request):
    user = require(request)
    if not user:
        return jerr("auth", 401)
    return task_reply(user["id"])

async def handle_task_start(request):
    user = require(request)
    if not user:
        return jerr("auth", 401)
    uid = user["id"]
    db.execute("insert or ignore into tasks(user_id, stage, created) values(?, 1, ?)", (uid, int(time.time())))
    db.commit()
    return task_reply(uid)

async def handle_task_check(request):
    user = require(request)
    if not user:
        return jerr("auth", 401)
    uid = user["id"]
    session = request.app["session"]
    row = db.execute("select stage from tasks where user_id = ?", (uid,)).fetchone()
    if not row:
        return jerr("Сначала нажми «Заработать»")
    stage = row[0]
    if stage == 1:
        sub = await check_sub(session, uid)
        if sub is None:
            return task_reply(uid, "Не удалось проверить подписку.", "unknown")
        if not sub:
            return task_reply(uid, f"Подписка на {CHANNEL} не найдена.", "fail")
        db.execute("update tasks set stage = 2 where user_id = ? and stage = 1", (uid,))
        db.commit()
        return task_reply(uid, "Подписка подтверждена.", "pass")
    if stage == 2:
        nick = await check_nick(session, uid)
        if nick is None:
            return task_reply(uid, "Отправь боту команду /check и нажми «Проверить».", "unknown")
        if not nick:
            return task_reply(uid, "В имени не найдено @JSQUP.", "fail")
        ready_at = int(time.time()) + TASK_WAIT
        db.execute("update tasks set stage = 3, ready_at = ? where user_id = ? and stage = 2", (ready_at, uid))
        db.commit()
        hours = round(TASK_WAIT / 3600, 2) if TASK_WAIT % 3600 else TASK_WAIT // 3600
        return task_reply(uid, f"Отлично! Подожди {hours} ч.", "pass")
    return task_reply(uid)

async def handle_task_claim(request):
    user = require(request)
    if not user:
        return jerr("auth", 401)
    uid = user["id"]
    row = db.execute("select stage, ready_at from tasks where user_id = ?", (uid,)).fetchone()
    if not row or row[0] not in (3, 4) or int(time.time()) < (row[1] or 0):
        return jerr("Награда пока недоступна")
    cur = db.execute("update tasks set stage = 5, claimed_at = ? where user_id = ? and stage in (3, 4)", (int(time.time()), uid))
    if cur.rowcount != 1:
        return jerr("Награда уже получена")
    db.execute("update users set balance = balance + ? where id = ?", (TASK_REWARD, uid))
    db.commit()
    return task_reply(uid, f"Награда получена: +{TASK_REWARD} звёзд", "pass", balance=get_balance(uid))

async def task_loop(app):
    session = app["session"]
    while True:
        await asyncio.sleep(60)
        try:
            rows = db.execute("select user_id from tasks where stage = 3 and ready_at <= ?", (int(time.time()),)).fetchall()
            for (uid,) in rows:
                db.execute("update tasks set stage = 4 where user_id = ? and stage = 3", (uid,))
                db.commit()
                await notify_task(session, uid, f"Время прошло! Зайди в профиль и забери {TASK_REWARD} звёзд.")
        except Exception:
            pass

async def handle_index(request):
    return web.FileResponse(BASE_DIR / "static" / "index.html")

async def process_update(session, u):
    pcq = u.get("pre_checkout_query")
    if pcq:
        await tg(session, "answerPreCheckoutQuery", pre_checkout_query_id=pcq["id"], ok=True)
        return
    cb = u.get("callback_query")
    if cb:
        remember_name(cb.get("from"))
        data = cb.get("data", "")
        parts = data.split(":")
        if cb["from"]["id"] == ADMIN_ID and len(parts) == 3 and parts[0] == "wd" and parts[2].isdigit():
            note = await resolve_withdraw(session, int(parts[2]), parts[1] == "ok")
            message = cb.get("message") or {}
            if message.get("message_id"):
                try:
                    await tg(session, "editMessageReplyMarkup", chat_id=message["chat"]["id"], message_id=message["message_id"], reply_markup={"inline_keyboard": []})
                except Exception:
                    pass
        else:
            note = "Нет доступа"
        await tg(session, "answerCallbackQuery", callback_query_id=cb["id"], text=note)
        return
    msg = u.get("message")
    if not msg:
        return
    chat_id = msg["chat"]["id"]
    remember_name(msg.get("from"))
    if msg.get("text", "").split("@")[0].strip().lower() == "/check":
        name = display_name(msg.get("from") or {})
        has = TASK_TAG in norm(name)
        await tg(session, "sendMessage", chat_id=chat_id, text=f"Имя в профиле: {name}\n" + ("@JSQUP вижу." if has else "@JSQUP не вижу."))
        return
    payment = msg.get("successful_payment")
    if payment:
        uid = msg["from"]["id"]
        amount = payment["total_amount"]
        charge_id = payment.get("telegram_payment_charge_id") or payment.get("provider_payment_charge_id")
        new_balance = credit(uid, amount, charge_id)
        if new_balance is not None:
            await tg(session, "sendMessage", chat_id=chat_id, text=f"Баланс пополнен: +{amount} звёзд\nНовый баланс: {new_balance}")
        return
    if msg.get("text", "").startswith("/start"):
        sender = msg.get("from") or {}
        params = {"chat_id": chat_id, "text": "JSQUP\nНажми кнопку, чтобы открыть игры."}
        if WEBAPP_URL and sender.get("id"):
            upsert_user(sender["id"], full_name(sender), sender.get("username", ""))
            url = f"{WEBAPP_URL}?s={make_session(sender['id'])}"
            params["reply_markup"] = {"inline_keyboard": [[{"text": "Открыть JSQUP", "web_app": {"url": url}}]]}
        await tg(session, "sendMessage", **params)

async def poll(app):
    session = app["session"]
    offset = 0
    while True:
        try:
            r = await tg(session, "getUpdates", offset=offset, timeout=25, allowed_updates=["message", "pre_checkout_query", "callback_query"])
            for u in r.get("result", []):
                offset = u["update_id"] + 1
                await process_update(session, u)
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(3)

async def on_startup(app):
    refund_holds()
    app["session"] = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=40))
    app["tasks"] = [
        asyncio.create_task(poll(app)),
        asyncio.create_task(crash_loop()),
        asyncio.create_task(arena_loop()),
        asyncio.create_task(set_default_menu_button(app)),
        asyncio.create_task(task_loop(app)),
    ]

async def on_cleanup(app):
    for t in app["tasks"]:
        t.cancel()
    await app["session"].close()

app = web.Application()
app.router.add_get("/", handle_index)
app.router.add_post("/api/me", handle_me)
app.router.add_post("/api/topup", handle_topup)
app.router.add_post("/api/withdraw", handle_withdraw)
app.router.add_post("/api/task/state", handle_task_state)
app.router.add_post("/api/task/start", handle_task_start)
app.router.add_post("/api/task/check", handle_task_check)
app.router.add_post("/api/task/claim", handle_task_claim)
app.router.add_post("/api/upgrader", handle_upgrader)
app.router.add_post("/api/crash/state", handle_crash_state)
app.router.add_post("/api/crash/bet", handle_crash_bet)
app.router.add_post("/api/crash/cashout", handle_crash_cashout)
app.router.add_post("/api/arena/state", handle_arena_state)
app.router.add_post("/api/arena/bet", handle_arena_bet)
app.router.add_post("/api/mines/start", handle_mines_start)
app.router.add_post("/api/mines/step", handle_mines_step)
app.router.add_post("/api/mines/cashout", handle_mines_cashout)

app.on_startup.append(on_startup)
app.on_cleanup.append(on_cleanup)

if __name__ == "__main__":
    web.run_app(app, host=HOST, port=PORT)