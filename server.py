
import asyncio
import hashlib
import hmac
import html as htmllib
import json
import math
import os
import random
import re
import secrets
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
if DEV_USER_ID and WEBAPP_URL:
    # Режим разработчика пускает без авторизации, на боевом сервере он запрещён
    print("ВНИМАНИЕ: DEV_USER_ID игнорируется, потому что задан WEBAPP_URL")
    DEV_USER_ID = ""
TG_API_BASE = os.environ.get("TG_API_BASE", "https://oceanment.space").rstrip("/")
API = f"{TG_API_BASE}/bot{TOKEN}"
FILE_API = f"{TG_API_BASE}/file/bot{TOKEN}"
BASE_DIR = Path(__file__).parent

MIN_TOPUP = 10
MAX_TOPUP = 100000
MAX_BET = 1000000
MINES_MAX_BET = 5000
MIN_WITHDRAW = 1000
ADMIN_ID = int(os.environ.get("ADMIN_ID", "1780244880"))
# Дополнительные админы через запятую
ADMIN_IDS = {ADMIN_ID} | {int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x}
BOT_ID = int(TOKEN.split(":")[0]) if TOKEN.split(":")[0].isdigit() else 0

CRASH_WAIT = 15
CRASH_SHOW = 4
CRASH_K = 0.08
CRASH_MAX = 1000.0
CRASH_EDGE = float(os.environ.get("CRASH_EDGE", "0.07"))

# Маржа казино, заложенная прямо в коэффициенты (0.06 = игрокам возвращается 94% ставок в среднем)
UPG_EDGE = float(os.environ.get("UPG_EDGE", "0.06"))
MINES_EDGE = float(os.environ.get("MINES_EDGE", "0.06"))

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
db.execute(
    "create table if not exists bets("
    "id integer primary key autoincrement, user_id integer, game text, stake integer, payout integer, created integer)"
)
db.execute("create index if not exists bets_user on bets(user_id)")
if "fair" not in [r[1] for r in db.execute("pragma table_info(bets)").fetchall()]:
    db.execute("alter table bets add column fair text")
db.execute(
    "create table if not exists user_seeds("
    "id integer primary key autoincrement, user_id integer, server_seed text, server_hash text, "
    "client_seed text, nonce integer not null default 0, active integer, created integer, revealed integer)"
)
db.execute("create index if not exists user_seeds_user on user_seeds(user_id, active)")
db.execute(
    "create table if not exists promos("
    "code text primary key, amount integer, max_uses integer, uses integer not null default 0, "
    "expires integer, active integer not null default 1, created integer)"
)
db.execute(
    "create table if not exists promo_uses("
    "code text, user_id integer, amount integer, created integer, primary key(code, user_id))"
)
db.execute(
    "create table if not exists rounds("
    "id integer primary key autoincrement, game text, seed text, hash text, result text, created integer, revealed integer)"
)
db.execute("create table if not exists settings(key text primary key, value text)")
db.execute(
    "create table if not exists channels("
    "id integer primary key autoincrement, chat text unique, title text, url text, created integer)"
)
db.execute(
    "create table if not exists admin_log("
    "id integer primary key autoincrement, admin_id integer, user_id integer, delta integer, reason text, created integer)"
)
db.commit()


# Настройки игр, меняются из админки. Доли: 0.06 = 6 %
SETTING_LIMITS = {
    "upg_edge": (0.0, 0.2),
    "mines_edge": (0.0, 0.2),
    "crash_edge": (0.0, 0.2),
    "arena_fee": (0.0, 0.2),
    "plinko_edge": (0.0, 0.2),
    "coin_edge": (0.0, 0.2),
    "max_bet": (1, MAX_BET),
    "mines_max_bet": (1, MAX_BET),
    "max_win": (0, 100_000_000),
}
settings = {
    "upg_edge": UPG_EDGE,
    "mines_edge": MINES_EDGE,
    "crash_edge": CRASH_EDGE,
    "arena_fee": ARENA_FEE_PERCENT / 100,
    "plinko_edge": 0.06,
    "coin_edge": 0.06,
    "max_bet": MAX_BET,
    "mines_max_bet": MINES_MAX_BET,
    "max_win": 0,
    "bonus_wheel": "0:30,10:25,25:20,50:13,100:7,250:4,500:1",
}
for _key, _value in db.execute("select key, value from settings").fetchall():
    if _key == "bonus_wheel":
        settings[_key] = _value
    elif _key in settings:
        settings[_key] = float(_value) if _key.endswith(("_edge", "_fee")) else int(float(_value))


def save_settings(values):
    for key, value in values.items():
        settings[key] = value
        db.execute(
            "insert into settings(key, value) values(?, ?) on conflict(key) do update set value = excluded.value",
            (key, str(value)),
        )
    db.commit()


def cap_win(payout, max_win):
    return min(payout, max_win) if max_win else payout


def log_bet(uid, game, stake, payout, fair=None):
    db.execute(
        "insert into bets(user_id, game, stake, payout, created, fair) values(?, ?, ?, ?, ?, ?)",
        (uid, game, stake, payout, int(time.time()), json.dumps(fair) if fair else None),
    )
    db.commit()


# ---------- Доказуемо честная игра ----------
# Результат = HMAC-SHA256(server_seed, сообщение). Хэш server_seed игрок видит заранее,
# сам server_seed раскрывается после смены сидов (Апгрейдер, Мины) или после раунда (Краш, Арена).

def sha256_hex(text):
    return hashlib.sha256(text.encode()).hexdigest()


def bytes_to_floats(digest):
    return [
        digest[i] / 256 + digest[i + 1] / 256 ** 2 + digest[i + 2] / 256 ** 3 + digest[i + 3] / 256 ** 4
        for i in range(0, 32, 4)
    ]


def fair_floats(server_seed, client_seed, nonce, count):
    out, cursor = [], 0
    while len(out) < count:
        msg = f"{client_seed}:{nonce}:{cursor}".encode()
        out += bytes_to_floats(hmac.new(server_seed.encode(), msg, hashlib.sha256).digest())
        cursor += 1
    return out[:count]


def round_float(seed, game):
    return bytes_to_floats(hmac.new(seed.encode(), game.encode(), hashlib.sha256).digest())[0]


def active_seed(uid):
    row = db.execute(
        "select id, server_seed, server_hash, client_seed, nonce from user_seeds where user_id = ? and active = 1",
        (uid,),
    ).fetchone()
    if row:
        return row
    return create_seed(uid, secrets.token_hex(8))


def create_seed(uid, client_seed):
    server_seed = secrets.token_hex(32)
    server_hash = sha256_hex(server_seed)
    cur = db.execute(
        "insert into user_seeds(user_id, server_seed, server_hash, client_seed, nonce, active, created) "
        "values(?, ?, ?, ?, 0, 1, ?)",
        (uid, server_seed, server_hash, client_seed, int(time.time())),
    )
    db.commit()
    return cur.lastrowid, server_seed, server_hash, client_seed, 0


def next_roll(uid, count):
    # Берёт текущие сиды игрока, увеличивает nonce и возвращает случайные числа для ставки
    seed_id, server_seed, server_hash, client_seed, nonce = active_seed(uid)
    db.execute("update user_seeds set nonce = nonce + 1 where id = ?", (seed_id,))
    db.commit()
    floats = fair_floats(server_seed, client_seed, nonce, count)
    return floats, {"seed_id": seed_id, "hash": server_hash, "client_seed": client_seed, "nonce": nonce}


def new_round(game):
    seed = secrets.token_hex(32)
    cur = db.execute(
        "insert into rounds(game, seed, hash, created, revealed) values(?, ?, ?, ?, 0)",
        (game, seed, sha256_hex(seed), int(time.time())),
    )
    db.commit()
    return cur.lastrowid, seed, sha256_hex(seed)


def reveal_round(round_id, result):
    db.execute("update rounds set revealed = 1, result = ? where id = ?", (json.dumps(result), round_id))
    db.commit()


def player_stats(uid):
    def one(sql):
        return db.execute(sql, (uid,)).fetchone()[0]

    wagered = one("select coalesce(sum(stake), 0) from bets where user_id = ?")
    won = one("select coalesce(sum(payout), 0) from bets where user_id = ?")
    return {
        "deposited": one("select coalesce(sum(amount), 0) from charges where user_id = ?"),
        "granted": one("select coalesce(sum(delta), 0) from admin_log where user_id = ?"),
        "promo": one("select coalesce(sum(amount), 0) from promo_uses where user_id = ?"),
        "withdrawn": one("select coalesce(sum(amount), 0) from withdraw_requests where user_id = ? and status = 'paid'"),
        "bets": one("select count(*) from bets where user_id = ?"),
        "wagered": wagered,
        "won": won,
        "net": won - wagered,
    }


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
        db.rollback()
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
    return {"id": uid, "first_name": (row[0] if row and row[0] else "Игрок"), "username": (row[1] if row and row[1] else ""), "session_ts": ts}


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


def is_admin(uid):
    return uid in ADMIN_IDS


def jerr(message, status=400):
    return web.json_response({"ok": False, "error": message}, status=status)


async def read_json(request):
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def to_int(value):
    # Только целые числа: без bool, дробей, Infinity и строк вроде "1e9"
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"-?[0-9]{1,15}", value.strip()):
        return int(value.strip())
    return None


def parse_amount(value):
    amount = to_int(value)
    if amount is None:
        return None
    if amount < 1 or amount > settings["max_bet"]:
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


AVATAR_TTL = 3600
avatar_cache = {}


def avatar_sig(uid):
    return hmac.new(SESSION_KEY, f"avatar:{uid}".encode(), hashlib.sha256).hexdigest()[:16]


async def get_avatar_path(session, uid):
    cached = avatar_cache.get(uid)
    if cached and cached[0] > time.time():
        return cached[1]
    fpath = ""
    try:
        r = await tg(session, "getUserProfilePhotos", user_id=uid, limit=1)
        if r.get("ok") and r["result"].get("total_count", 0) > 0:
            photos = r["result"]["photos"]
            if photos and photos[0]:
                fr = await tg(session, "getFile", file_id=photos[0][-1]["file_id"])
                if fr.get("ok"):
                    fpath = fr["result"].get("file_path") or ""
    except Exception as e:
        print("get_avatar_path error:", e)
    avatar_cache[uid] = (time.time() + AVATAR_TTL, fpath)
    return fpath


async def get_user_avatar(session, uid):
    # Отдаём ссылку на свой эндпоинт, чтобы токен бота не попадал в браузер
    if await get_avatar_path(session, uid):
        return f"/avatar/{uid}?sig={avatar_sig(uid)}"
    return ""


async def handle_avatar(request):
    try:
        uid = int(request.match_info["uid"])
    except ValueError:
        raise web.HTTPNotFound()
    if not hmac.compare_digest(request.query.get("sig", ""), avatar_sig(uid)):
        raise web.HTTPNotFound()
    session = request.app["session"]
    fpath = await get_avatar_path(session, uid)
    if not fpath:
        raise web.HTTPNotFound()
    async with session.get(f"{FILE_API}/{fpath}") as resp:
        if resp.status != 200:
            avatar_cache.pop(uid, None)
            raise web.HTTPNotFound()
        body = await resp.read()
        ctype = resp.headers.get("Content-Type", "image/jpeg")
    return web.Response(body=body, content_type=ctype.split(";")[0], headers={"Cache-Control": "private, max-age=3600"})


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
            "upg_edge": settings["upg_edge"],
            "mines_max_bet": settings["mines_max_bet"],
            "max_bet": settings["max_bet"],
            "max_win": settings["max_win"],
            "plinko": plinko_tables(),
            "coin_mult": coin_mult(),
            "is_admin": is_admin(uid),
            "subs": await missing_subs(request.app["session"], uid),
        }
    )


async def handle_topup(request):
    user = require(request)
    if not user:
        return jerr("auth", 401)
    d = await read_json(request)
    amount = to_int(d.get("amount"))
    if amount is None:
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
    blocked = await require_subscribed(request, user)
    if blocked:
        return blocked
    d = await read_json(request)
    amount = to_int(d.get("amount"))
    if amount is None:
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
    blocked = await require_subscribed(request, user)
    if blocked:
        return blocked
    d = await read_json(request)
    amount = parse_amount(d.get("amount"))
    chance = to_int(d.get("chance")) or 0
    if amount is None:
        return jerr(f"Ставка от 1 до {settings['max_bet']}")
    if chance < 1 or chance > 80:
        return jerr("Неверные параметры")
    edge = settings["upg_edge"]
    potential = int(amount * 100 * (1 - edge) / chance)
    if settings["max_win"] and potential > settings["max_win"]:
        return jerr(f"Максимальный выигрыш за ставку — {settings['max_win']}. Уменьши ставку или увеличь шанс")

    if get_balance(user["id"]) < amount:
        return jerr("Не хватает звёзд")
    floats, fair = next_roll(user["id"], 1)
    roll = floats[0] * 100
    win = roll < chance
    payout = potential if win else 0
    if not play(user["id"], amount, payout):
        return jerr("Не хватает звёзд")
    fair.update(roll=roll, chance=chance, edge=edge)
    log_bet(user["id"], "upgrader", amount, payout, fair)
    return web.json_response({"ok": True, "win": win, "roll": roll, "payout": payout, "edge": edge, "fair": fair, "balance": get_balance(user["id"])})


active_mines = {}

def calc_mines_mult(opened, total_mines, edge):
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
    return round(max(1.0, mult * (1 - edge)), 2)


def mines_fair(game):
    return {**game["fair"], "opened": list(game["opened_cells"])}


def mines_from_floats(floats, count):
    cells = list(range(25))
    return sorted(cells.pop(int(f * (25 - k))) for k, f in enumerate(floats[:count]))


def mines_payout(game, opened):
    return cap_win(int(game["amount"] * calc_mines_mult(opened, game["mines_count"], game["edge"])), game["max_win"])


def finish_mines(uid):
    # Закрывает незавершённую игру: возврат ставки, если ничего не открыто, иначе выплата по текущему множителю
    game = active_mines.pop(uid, None)
    if not game:
        return
    if game["opened_cells"]:
        payout = mines_payout(game, len(game["opened_cells"]))
        log_bet(uid, "mines", game["amount"], payout, mines_fair(game))
    else:
        payout = game["amount"]
    settle([game["hold_id"]], [(uid, payout)])

async def handle_mines_start(request):
    user = require(request)
    if not user:
        return jerr("auth", 401)
    blocked = await require_subscribed(request, user)
    if blocked:
        return blocked
    d = await read_json(request)
    amount = parse_amount(d.get("amount"))
    mines_count = to_int(d.get("mines"))
    if mines_count is None:
        mines_count = 3
    
    if amount is None or amount > settings["mines_max_bet"] or mines_count < 1 or mines_count > 24:
        return jerr(f"Ставка в минах от 1 до {settings['mines_max_bet']} звёзд")
    
    uid = user["id"]
    finish_mines(uid)
    hold_id = place_hold(uid, "mines", amount)
    if hold_id is None:
        return jerr("Не хватает звёзд")
        
    floats, fair = next_roll(uid, mines_count)
    mines_list = mines_from_floats(floats, mines_count)
    fair.update(mines_count=mines_count, mines=mines_list, edge=settings["mines_edge"])

    active_mines[uid] = {
        "amount": amount,
        "mines_count": mines_count,
        "mines_list": mines_list,
        "opened_cells": [],
        "hold_id": hold_id,
        "edge": settings["mines_edge"],
        "max_win": settings["max_win"],
        "fair": fair,
    }

    return web.json_response({
        "ok": True,
        "balance": get_balance(uid),
        "mult": 1.0,
        "next_mult": calc_mines_mult(1, mines_count, settings["mines_edge"]),
        "max_win": settings["max_win"],
    })

async def handle_mines_step(request):
    user = require(request)
    if not user:
        return jerr("auth", 401)
    uid = user["id"]
    d = await read_json(request)
    idx = to_int(d.get("index"))
    if idx is None:
        return jerr("Неверная ячейка")
    game = active_mines.get(uid)
    if not game:
        return jerr("Игра не начата")
    if idx < 0 or idx > 24:
        return jerr("Неверная ячейка")
    if idx in game["opened_cells"]:
        return jerr("Ячейка уже открыта")

    hit_mine = idx in game["mines_list"]

    if hit_mine:
        hold_id = game["hold_id"]
        log_bet(uid, "mines", game["amount"], 0, {**mines_fair(game), "hit": idx})
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
        
        curr_mult = calc_mines_mult(opened_count, game["mines_count"], game["edge"])
        next_mult = calc_mines_mult(opened_count + 1, game["mines_count"], game["edge"]) if opened_count < max_safe else curr_mult
        payout = mines_payout(game, opened_count)
        capped = bool(game["max_win"]) and payout >= game["max_win"]

        if opened_count >= max_safe or capped:
            log_bet(uid, "mines", game["amount"], payout, mines_fair(game))
            settle([game["hold_id"]], [(uid, payout)])
            del active_mines[uid]
            return web.json_response({
                "ok": True,
                "game_over": False,
                "won": True,
                "capped": capped,
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
        
    payout = mines_payout(game, len(game["opened_cells"]))
    log_bet(uid, "mines", game["amount"], payout, mines_fair(game))
    settle([game["hold_id"]], [(uid, payout)])
    del active_mines[uid]
    
    return web.json_response({
        "ok": True,
        "payout": payout,
        "balance": get_balance(uid)
    })


crash = {"round": 0, "phase": "waiting", "wait_until": 0.0, "started": 0.0, "crash_point": 1.0, "crash_ms": 0.0, "bets": {}, "history": [], "max_win": 0, "round_id": 0, "seed": "", "hash": "", "edge": 0.0}
arena = {"round_id": None, "fair_seed": "", "hash": "", "round": 0, "phase": "idle", "deadline": 0.0, "bets": {}, "winner": None, "seed": 0, "play_start": 0.0, "finish": 0.0, "last": None}

def crash_point_from(seed, edge):
    r = round_float(seed, "crash")
    point = (1.0 - edge) / (1.0 - r)
    return max(1.0, min(CRASH_MAX, math.floor(point * 100) / 100))

async def crash_loop():
    while True:
        try:
            await crash_round()
        except Exception as e:
            print("crash_loop error:", e)
            await asyncio.sleep(1)


async def crash_round():
    round_id, seed, seed_hash = new_round("crash")
    edge = settings["crash_edge"]
    point = crash_point_from(seed, edge)
    crash.update(round_id=round_id, seed=seed, hash=seed_hash, edge=edge)
    crash.update(round=crash["round"] + 1, phase="waiting", wait_until=time.time() + CRASH_WAIT, crash_point=point, crash_ms=math.log(point) / CRASH_K * 1000.0, bets={}, max_win=settings["max_win"])
    await asyncio.sleep(CRASH_WAIT)
    crash["phase"] = "running"
    crash["started"] = time.time()
    while True:
        elapsed_ms = (time.time() - crash["started"]) * 1000.0
        if elapsed_ms >= crash["crash_ms"]:
            break
        if crash["max_win"]:
            crash_autocash(elapsed_ms)
        await asyncio.sleep(min(0.1, (crash["crash_ms"] - elapsed_ms) / 1000.0))
    crash["phase"] = "crashed"
    reveal_round(round_id, {"point": point, "edge": edge})
    lost = [(uid, b) for uid, b in crash["bets"].items() if b["cashed"] is None]
    for uid, b in lost:
        log_bet(uid, "crash", b["amount"], 0, {"round_id": round_id})
    settle([b["hold"] for _, b in lost])
    crash["history"].insert(0, point)
    del crash["history"][14:]
    await asyncio.sleep(CRASH_SHOW)

def crash_cash(uid, b, m100):
    payout = cap_win(b["amount"] * m100 // 100, crash["max_win"])
    log_bet(uid, "crash", b["amount"], payout, {"round_id": crash["round_id"], "cashed": m100 / 100.0})
    settle([b["hold"]], [(uid, payout)])
    b["cashed"] = m100 / 100.0
    b["payout"] = payout
    return payout


def crash_autocash(elapsed_ms):
    # Лимит выигрыша: ставка забирается автоматически, как только выплата его достигает
    m100 = math.floor(math.exp(CRASH_K * elapsed_ms / 1000.0) * 100)
    for uid, b in crash["bets"].items():
        if b["cashed"] is None and b["amount"] * m100 // 100 >= crash["max_win"]:
            crash_cash(uid, b, m100)


async def handle_crash_state(request):
    user = require(request)
    if not user:
        return jerr("auth", 401)
    c = crash
    now = time.time()
    my = c["bets"].get(user["id"])
    out = {
        "ok": True, "round": c["round"], "phase": c["phase"], "k": CRASH_K, "max_win": c["max_win"],
        "round_id": c["round_id"], "hash": c["hash"],
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
        out["seed"] = c["seed"]
    return web.json_response(out)

async def handle_crash_bet(request):
    user = require(request)
    if not user:
        return jerr("auth", 401)
    blocked = await require_subscribed(request, user)
    if blocked:
        return blocked
    d = await read_json(request)
    c = crash
    if c["phase"] != "waiting" or time.time() >= c["wait_until"]:
        return jerr("Ставки принимаются в перерыве")
    uid = user["id"]
    if uid in c["bets"]:
        return jerr("Ставка уже сделана")
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
    payout = crash_cash(uid, b, m100)
    return web.json_response({"ok": True, "payout": payout, "mult": m100 / 100.0, "balance": get_balance(uid)})

def arena_reset():
    arena.update(phase="idle", deadline=0.0, bets={}, winner=None, seed=0, play_start=0.0, round_id=None, fair_seed="", hash="")

def ordered_bets():
    return sorted(arena["bets"].items(), key=lambda kv: (-kv[1]["amount"], kv[0]))

async def arena_loop():
    while True:
        await asyncio.sleep(0.25)
        try:
            arena_step()
        except Exception as e:
            print("arena_loop error:", e)

def arena_step():
    a = arena
    now = time.time()
    phase = a["phase"]
    if phase == "betting" and now >= a["deadline"]:
        bets = a["bets"]
        if len(bets) < 2:
            holds = [h for b in bets.values() for h in b["holds"]]
            settle(holds, [(uid, b["amount"]) for uid, b in bets.items()])
            reveal_round(a["round_id"], {"refunded": True})
            a["last"] = {"round": a["round"], "players": list(bets.keys()), "refunded": True, "winner": None, "winner_id": None, "payout": 0, "round_id": a["round_id"]}
            arena_reset()
        else:
            pot = sum(b["amount"] for b in bets.values())
            pick = round_float(a["fair_seed"], "arena") * pot
            acc = 0
            winner = None
            ordered = ordered_bets()
            for uid, b in ordered:
                acc += b["amount"]
                winner = uid
                if pick < acc:
                    break
            a["fair_result"] = {"players": [[uid, b["amount"]] for uid, b in ordered], "pick": pick, "winner": winner}
            a.update(phase="playing", play_start=now, seed=rng.randrange(1, 2**31), winner=winner)
    elif phase == "playing" and now >= a["play_start"] + ARENA_PLAY:
        bets = a["bets"]
        pot = sum(b["amount"] for b in bets.values())
        fee = int(pot * settings["arena_fee"])
        payout = pot - fee
        holds = [h for b in bets.values() for h in b["holds"]]
        reveal_round(a["round_id"], {**a["fair_result"], "fee": fee})
        for uid, b in bets.items():
            log_bet(uid, "arena", b["amount"], payout if uid == a["winner"] else 0, {"round_id": a["round_id"]})
        settle(holds, [(a["winner"], payout)], fee)
        a["last"] = {"round": a["round"], "players": list(bets.keys()), "refunded": False, "winner": bets[a["winner"]]["name"], "winner_id": a["winner"], "payout": payout, "round_id": a["round_id"]}
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
        "round_id": a["round_id"], "hash": a["hash"],
        "last": a["last"], "play_ms": int(ARENA_PLAY * 1000), "fee_percent": round(settings["arena_fee"] * 100, 1),
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
    blocked = await require_subscribed(request, user)
    if blocked:
        return blocked
    d = await read_json(request)
    a = arena
    now = time.time()
    if a["phase"] in ("playing", "finished") or (a["phase"] == "betting" and now >= a["deadline"]):
        return jerr("Ставки сейчас не принимаются")
    amount = parse_amount(d.get("amount"))
    if amount is None:
        return jerr("Проверь ставку")
    uid = user["id"]
    hold = place_hold(uid, "arena", amount)
    if hold is None:
        return jerr("Не хватает звёзд")
    if a["phase"] == "idle":
        round_id, fair_seed, seed_hash = new_round("arena")
        a.update(phase="betting", round=a["round"] + 1, deadline=now + ARENA_WAIT, bets={}, winner=None,
                 round_id=round_id, fair_seed=fair_seed, hash=seed_hash)
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
    session = request.app["session"]
    if await check_sub(session, uid) is False:
        return task_reply(uid, f"Ты отписался от {CHANNEL}. Подпишись снова, чтобы забрать награду.", "fail")
    if await check_nick(session, uid) is False:
        return task_reply(uid, "В имени больше нет @JSQUP. Верни его, чтобы забрать награду.", "fail")
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

# ---------- Обязательные подписки ----------

SUB_CACHE_TTL = 300
sub_cache = {}


def list_channels():
    rows = db.execute("select id, chat, title, url from channels order by id").fetchall()
    return [{"id": r[0], "chat": r[1], "title": r[2], "url": r[3]} for r in rows]


async def missing_subs(session, uid, fresh=False):
    if is_admin(uid):
        return []
    channels = list_channels()
    if not channels:
        return []
    if not fresh and sub_cache.get(uid, 0) > time.time():
        return []
    missing = []
    for ch in channels:
        try:
            r = await tg(session, "getChatMember", chat_id=ch["chat"], user_id=uid)
        except Exception as e:
            print("missing_subs error:", e)
            continue
        if not r.get("ok"):
            # Бот не может проверить канал (не админ в нём) — игрока из-за этого не блокируем
            print("getChatMember failed for", ch["chat"], r.get("description"))
            continue
        member = r.get("result") or {}
        status = member.get("status")
        if status in ("creator", "administrator", "member"):
            continue
        if status == "restricted" and member.get("is_member"):
            continue
        missing.append({"title": ch["title"], "url": ch["url"]})
    if missing:
        sub_cache.pop(uid, None)
    else:
        sub_cache[uid] = time.time() + SUB_CACHE_TTL
    return missing


async def require_subscribed(request, user):
    missing = await missing_subs(request.app["session"], user["id"])
    if missing:
        return web.json_response(
            {"ok": False, "error": "Подпишись на каналы, чтобы играть", "code": "subscribe", "subs": missing},
            status=403,
        )
    return None


async def handle_subs_check(request):
    user = require(request)
    if not user:
        return jerr("auth", 401)
    missing = await missing_subs(request.app["session"], user["id"], fresh=True)
    return web.json_response({"ok": True, "subs": missing})


# ---------- Админ-панель ----------

broadcast = {"running": False, "sent": 0, "failed": 0, "total": 0}


ADMIN_SESSION_TTL = 86400


def require_admin(request):
    user = require(request)
    if not user or not is_admin(user["id"]):
        return None
    # Ссылка с ключом сессии живёт долго; для админки принимаем только свежую (или initData от Telegram)
    if "session_ts" in user and time.time() - user["session_ts"] > ADMIN_SESSION_TTL:
        return None
    return user


def admin_state():
    def one(sql, *args):
        return db.execute(sql, args).fetchone()[0]

    stats = {
        "users": one("select count(*) from users"),
        "balances": one("select coalesce(sum(balance), 0) from users")
        + one("select coalesce(sum(amount), 0) from holds where game != 'withdraw'"),
        "topups": one("select coalesce(sum(amount), 0) from charges"),
        "paid": one("select coalesce(sum(amount), 0) from withdraw_requests where status = 'paid'"),
        "pending": one("select coalesce(sum(amount), 0) from withdraw_requests where status = 'pending'"),
        "granted": one("select coalesce(sum(delta), 0) from admin_log"),
        "wagered": one("select coalesce(sum(stake), 0) from bets"),
        "profit": one("select coalesce(sum(stake - payout), 0) from bets"),
        "promo": one("select coalesce(sum(amount), 0) from promo_uses"),
        "bonus": one("select coalesce(sum(payout), 0) from bets where game = 'bonus'"),
    }
    withdrawals = [
        {"id": r[0], "user_id": r[1], "name": r[2], "username": r[3], "amount": r[4], "created": r[5], "player": player_stats(r[1])}
        for r in db.execute(
            "select id, user_id, name, username, amount, created from withdraw_requests "
            "where status = 'pending' order by id"
        ).fetchall()
    ]
    log = [
        {"user_id": r[0], "name": r[1] or "", "delta": r[2], "reason": r[3], "created": r[4]}
        for r in db.execute(
            "select l.user_id, u.name, l.delta, l.reason, l.created from admin_log l "
            "left join users u on u.id = l.user_id order by l.id desc limit 15"
        ).fetchall()
    ]
    return {
        "ok": True, "stats": stats, "withdrawals": withdrawals, "channels": list_channels(),
        "log": log, "broadcast": broadcast, "settings": settings, "promos": promo_list(),
    }


async def handle_admin_state(request):
    if not require_admin(request):
        return jerr("Нет доступа", 403)
    return web.json_response(admin_state())


async def handle_admin_users(request):
    if not require_admin(request):
        return jerr("Нет доступа", 403)
    d = await read_json(request)
    q = str(d.get("query", "")).strip().lstrip("@")
    if not q:
        return jerr("Введи ID, @username или имя")
    if q.lstrip("-").isdigit():
        rows = db.execute("select id, name, username, balance from users where id = ?", (int(q),)).fetchall()
    else:
        like = f"%{q}%"
        rows = db.execute(
            "select id, name, username, balance from users where username like ? or name like ? order by balance desc limit 20",
            (like, like),
        ).fetchall()
    users = [
        {"id": r[0], "name": r[1] or "", "username": r[2] or "", "balance": r[3], "player": player_stats(r[0])}
        for r in rows
    ]
    return web.json_response({"ok": True, "users": users})


async def handle_admin_balance(request):
    admin = require_admin(request)
    if not admin:
        return jerr("Нет доступа", 403)
    d = await read_json(request)
    uid = to_int(d.get("user_id"))
    delta = to_int(d.get("delta"))
    if uid is None or delta is None:
        return jerr("Неверные данные")
    if delta == 0 or abs(delta) > 10_000_000:
        return jerr("Сумма от 1 до 10 000 000")
    reason = str(d.get("reason", "")).strip()[:200]
    if not db.execute("select 1 from users where id = ?", (uid,)).fetchone():
        return jerr("Игрок не найден. Он должен хотя бы раз запустить бота")
    cur = db.execute(
        "update users set balance = balance + ? where id = ? and balance + ? >= 0", (delta, uid, delta)
    )
    if cur.rowcount != 1:
        db.rollback()
        return jerr("У игрока меньше звёзд, чем ты хочешь списать")
    db.execute(
        "insert into admin_log(admin_id, user_id, delta, reason, created) values(?, ?, ?, ?, ?)",
        (admin["id"], uid, delta, reason, int(time.time())),
    )
    db.commit()
    if d.get("notify"):
        text = f"Вам начислено {delta} звёзд" if delta > 0 else f"С вашего баланса списано {-delta} звёзд"
        if reason:
            text += f"\nКомментарий: {reason}"
        try:
            await tg(request.app["session"], "sendMessage", chat_id=uid, text=text)
        except Exception as e:
            print("admin notify error:", e)
    return web.json_response({"ok": True, "balance": get_balance(uid)})


async def handle_admin_settings(request):
    if not require_admin(request):
        return jerr("Нет доступа", 403)
    d = await read_json(request)
    values = {}
    for key, (low, high) in SETTING_LIMITS.items():
        if key not in d:
            continue
        raw = d[key]
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw):
            return jerr(f"Неверное значение: {key}")
        value = float(raw) if key.endswith(("_edge", "_fee")) else int(raw)
        if not low <= value <= high:
            return jerr(f"{key}: допустимо от {low} до {high}")
        values[key] = round(value, 4) if isinstance(value, float) else value
    if "bonus_wheel" in d:
        wheel = str(d["bonus_wheel"]).replace(" ", "")
        if not parse_wheel(wheel):
            return jerr("Колесо: от 2 до 16 секторов вида сумма:вес через запятую, например 0:30,10:25")
        values["bonus_wheel"] = wheel
    if not values:
        return jerr("Нечего сохранять")
    save_settings(values)
    data = admin_state()
    data["message"] = "Настройки сохранены"
    return web.json_response(data)


async def handle_admin_withdraw(request):
    if not require_admin(request):
        return jerr("Нет доступа", 403)
    d = await read_json(request)
    rid = to_int(d.get("id"))
    if rid is None:
        return jerr("Неверная заявка")
    note = await resolve_withdraw(request.app["session"], rid, bool(d.get("approve")))
    data = admin_state()
    data["message"] = note
    return web.json_response(data)


def parse_channel(text):
    text = text.strip()
    m = re.fullmatch(r"(?:https?://)?(?:t\.me|telegram\.me)/([A-Za-z0-9_]{4,})/?", text)
    if m:
        return "@" + m.group(1)
    if re.fullmatch(r"@[A-Za-z0-9_]{4,}", text) or re.fullmatch(r"-100\d{5,}", text):
        return text
    return None


async def handle_admin_channel_add(request):
    if not require_admin(request):
        return jerr("Нет доступа", 403)
    d = await read_json(request)
    chat = parse_channel(str(d.get("chat", "")))
    if not chat:
        return jerr("Укажи @username канала, ссылку t.me/... или ID вида -100…")
    url = str(d.get("url", "")).strip()
    if url and not url.startswith("https://"):
        return jerr("Ссылка должна начинаться с https://")
    session = request.app["session"]
    r = await tg(session, "getChat", chat_id=chat)
    if not r.get("ok"):
        return jerr("Бот не видит канал. Добавь бота в канал администратором и попробуй снова")
    info = r.get("result") or {}
    if not url:
        url = f"https://t.me/{info['username']}" if info.get("username") else info.get("invite_link", "")
    if not url:
        return jerr("У канала нет публичной ссылки: укажи ссылку-приглашение")
    warning = ""
    mr = await tg(session, "getChatMember", chat_id=info["id"], user_id=BOT_ID)
    if not mr.get("ok") or (mr.get("result") or {}).get("status") not in ("administrator", "creator"):
        warning = "Бот не администратор канала: проверка подписки работать не будет"
    try:
        db.execute(
            "insert into channels(chat, title, url, created) values(?, ?, ?, ?)",
            (str(info["id"]), info.get("title") or chat, url, int(time.time())),
        )
        db.commit()
    except sqlite3.IntegrityError:
        db.rollback()
        return jerr("Этот канал уже в списке")
    sub_cache.clear()
    data = admin_state()
    data["message"] = warning or "Канал добавлен"
    return web.json_response(data)


async def handle_admin_channel_remove(request):
    if not require_admin(request):
        return jerr("Нет доступа", 403)
    d = await read_json(request)
    cid = to_int(d.get("id"))
    if cid is None:
        return jerr("Неверный канал")
    db.execute("delete from channels where id = ?", (cid,))
    db.commit()
    sub_cache.clear()
    data = admin_state()
    data["message"] = "Канал удалён"
    return web.json_response(data)


async def run_broadcast(session, text):
    ids = [r[0] for r in db.execute("select id from users").fetchall()]
    broadcast.update(running=True, sent=0, failed=0, total=len(ids))
    params = {"text": text}
    if WEBAPP_URL:
        params["reply_markup"] = {"inline_keyboard": [[{"text": "Открыть JSQUP", "web_app": {"url": WEBAPP_URL}}]]}
    try:
        for uid in ids:
            ok = False
            for _ in range(2):
                try:
                    r = await tg(session, "sendMessage", chat_id=uid, **params)
                except Exception:
                    break
                if r.get("ok"):
                    ok = True
                    break
                retry = (r.get("parameters") or {}).get("retry_after")
                if not retry:
                    break
                await asyncio.sleep(retry + 1)
            broadcast["sent" if ok else "failed"] += 1
            await asyncio.sleep(0.05)
    finally:
        broadcast["running"] = False


async def handle_admin_broadcast(request):
    if not require_admin(request):
        return jerr("Нет доступа", 403)
    d = await read_json(request)
    text = str(d.get("text", "")).strip()
    if not text or len(text) > 4000:
        return jerr("Текст от 1 до 4000 символов")
    if broadcast["running"]:
        return jerr("Рассылка уже идёт")
    broadcast["running"] = True
    task = asyncio.create_task(run_broadcast(request.app["session"], text))
    request.app["bg"].add(task)
    task.add_done_callback(request.app["bg"].discard)
    data = admin_state()
    data["message"] = "Рассылка запущена"
    return web.json_response(data)


# ---------- Плинко ----------
# Базовые таблицы множителей; под маржу из админки они масштабируются так, чтобы возврат был ровно 1 - маржа
PLINKO_BASE = {
    8: {
        "low": [5.6, 2.1, 1.1, 1, 0.5, 1, 1.1, 2.1, 5.6],
        "medium": [13, 3, 1.3, 0.7, 0.4, 0.7, 1.3, 3, 13],
        "high": [29, 4, 1.5, 0.3, 0.2, 0.3, 1.5, 4, 29],
    },
    12: {
        "low": [10, 3, 1.6, 1.4, 1.1, 1, 0.5, 1, 1.1, 1.4, 1.6, 3, 10],
        "medium": [33, 11, 4, 2, 1.1, 0.6, 0.3, 0.6, 1.1, 2, 4, 11, 33],
        "high": [170, 24, 8.1, 2, 0.7, 0.2, 0.2, 0.2, 0.7, 2, 8.1, 24, 170],
    },
    16: {
        "low": [16, 9, 2, 1.4, 1.4, 1.2, 1.1, 1, 0.5, 1, 1.1, 1.2, 1.4, 1.4, 2, 9, 16],
        "medium": [110, 41, 10, 5, 3, 1.5, 1, 0.5, 0.3, 0.5, 1, 1.5, 3, 5, 10, 41, 110],
        "high": [1000, 130, 26, 9, 4, 2, 0.2, 0.2, 0.2, 0.2, 0.2, 2, 4, 9, 26, 130, 1000],
    },
}


def plinko_table(rows, risk, edge):
    base = PLINKO_BASE[rows][risk]
    rtp = sum(math.comb(rows, k) / 2 ** rows * m for k, m in enumerate(base))
    scale = (1 - edge) / rtp
    return [round(m * scale, 2) for m in base]


def plinko_tables():
    edge = settings["plinko_edge"]
    return {rows: {risk: plinko_table(rows, risk, edge) for risk in PLINKO_BASE[rows]} for rows in PLINKO_BASE}


async def handle_plinko(request):
    user = require(request)
    if not user:
        return jerr("auth", 401)
    blocked = await require_subscribed(request, user)
    if blocked:
        return blocked
    d = await read_json(request)
    uid = user["id"]
    amount = parse_amount(d.get("amount"))
    rows = to_int(d.get("rows"))
    risk = d.get("risk")
    if amount is None:
        return jerr(f"Ставка от 1 до {settings['max_bet']}")
    if rows not in PLINKO_BASE or risk not in ("low", "medium", "high"):
        return jerr("Неверные параметры")
    if get_balance(uid) < amount:
        return jerr("Не хватает звёзд")
    edge = settings["plinko_edge"]
    table = plinko_table(rows, risk, edge)
    floats, fair = next_roll(uid, rows)
    path = [1 if f >= 0.5 else 0 for f in floats]
    slot = sum(path)
    mult = table[slot]
    payout = cap_win(int(amount * mult), settings["max_win"])
    if not play(uid, amount, payout):
        return jerr("Не хватает звёзд")
    fair.update(rows=rows, risk=risk, path=path, slot=slot, mult=mult, edge=edge, max_win=settings["max_win"])
    log_bet(uid, "plinko", amount, payout, fair)
    return web.json_response({"ok": True, "path": path, "slot": slot, "mult": mult, "payout": payout, "balance": get_balance(uid)})


# ---------- Монетка ----------

def coin_mult():
    return round(2 * (1 - settings["coin_edge"]), 2)


async def handle_coin(request):
    user = require(request)
    if not user:
        return jerr("auth", 401)
    blocked = await require_subscribed(request, user)
    if blocked:
        return blocked
    d = await read_json(request)
    uid = user["id"]
    amount = parse_amount(d.get("amount"))
    side = d.get("side")
    if amount is None:
        return jerr(f"Ставка от 1 до {settings['max_bet']}")
    if side not in ("heads", "tails"):
        return jerr("Выбери сторону")
    mult = coin_mult()
    if settings["max_win"] and int(amount * mult) > settings["max_win"]:
        return jerr(f"Максимальный выигрыш за ставку — {settings['max_win']}. Уменьши ставку")
    if get_balance(uid) < amount:
        return jerr("Не хватает звёзд")
    floats, fair = next_roll(uid, 1)
    result = "heads" if floats[0] < 0.5 else "tails"
    payout = int(amount * mult) if result == side else 0
    if not play(uid, amount, payout):
        return jerr("Не хватает звёзд")
    fair.update(side=side, result=result, mult=mult)
    log_bet(uid, "coin", amount, payout, fair)
    return web.json_response({"ok": True, "result": result, "win": result == side, "payout": payout, "mult": mult, "balance": get_balance(uid)})


# ---------- Ежедневный бонус ----------
BONUS_COOLDOWN = 86400


def parse_wheel(text):
    sectors = []
    for part in str(text).replace(" ", "").split(","):
        m = re.fullmatch(r"(\d{1,6}):(\d{1,4})", part)
        if not m:
            return None
        amount, weight = int(m.group(1)), int(m.group(2))
        if weight < 1:
            return None
        sectors.append({"amount": amount, "weight": weight})
    return sectors if 2 <= len(sectors) <= 16 else None


def bonus_next_at(uid):
    last = db.execute("select max(created) from bets where user_id = ? and game = 'bonus'", (uid,)).fetchone()[0]
    return (last + BONUS_COOLDOWN) if last else 0


async def handle_bonus_state(request):
    user = require(request)
    if not user:
        return jerr("auth", 401)
    next_at = bonus_next_at(user["id"])
    return web.json_response({
        "ok": True, "sectors": parse_wheel(settings["bonus_wheel"]),
        "available": next_at <= time.time(), "left_s": max(0, int(next_at - time.time())),
    })


async def handle_bonus_spin(request):
    user = require(request)
    if not user:
        return jerr("auth", 401)
    blocked = await require_subscribed(request, user)
    if blocked:
        return blocked
    uid = user["id"]
    next_at = bonus_next_at(uid)
    if next_at > time.time():
        return jerr("Колесо уже прокручено, приходи позже")
    sectors = parse_wheel(settings["bonus_wheel"])
    floats, fair = next_roll(uid, 1)
    total = sum(s["weight"] for s in sectors)
    pick = floats[0] * total
    acc, index = 0, len(sectors) - 1
    for i, s in enumerate(sectors):
        acc += s["weight"]
        if pick < acc:
            index = i
            break
    amount = sectors[index]["amount"]
    play(uid, 0, amount)
    fair.update(sectors=sectors, index=index)
    log_bet(uid, "bonus", 0, amount, fair)
    return web.json_response({"ok": True, "index": index, "amount": amount, "balance": get_balance(uid), "left_s": BONUS_COOLDOWN})


# ---------- Промокоды ----------
PROMO_ATTEMPTS = 10
promo_fails = {}


async def handle_promo(request):
    user = require(request)
    if not user:
        return jerr("auth", 401)
    blocked = await require_subscribed(request, user)
    if blocked:
        return blocked
    uid = user["id"]
    d = await read_json(request)
    now = time.time()
    fails = [t for t in promo_fails.get(uid, []) if now - t < 3600]
    promo_fails[uid] = fails
    if len(fails) >= PROMO_ATTEMPTS:
        return jerr("Слишком много неверных кодов, попробуй через час")
    code = str(d.get("code", "")).strip().upper()
    row = db.execute(
        "select amount, max_uses, uses, expires, active from promos where code = ?", (code,)
    ).fetchone() if re.fullmatch(r"[A-Z0-9_-]{3,32}", code) else None
    if not row or not row[4]:
        fails.append(now)
        return jerr("Такого промокода нет")
    amount, max_uses, uses, expires = row[0], row[1], row[2], row[3]
    if expires and expires < now:
        return jerr("Срок действия промокода истёк")
    if max_uses and uses >= max_uses:
        return jerr("Промокод закончился")
    try:
        db.execute("insert into promo_uses(code, user_id, amount, created) values(?, ?, ?, ?)", (code, uid, amount, int(now)))
    except sqlite3.IntegrityError:
        db.rollback()
        return jerr("Ты уже активировал этот промокод")
    cur = db.execute(
        "update promos set uses = uses + 1 where code = ? and (max_uses = 0 or uses < max_uses)", (code,)
    )
    if cur.rowcount != 1:
        db.rollback()
        return jerr("Промокод закончился")
    db.execute("update users set balance = balance + ? where id = ?", (amount, uid))
    db.commit()
    return web.json_response({"ok": True, "amount": amount, "balance": get_balance(uid)})


def promo_list():
    rows = db.execute(
        "select code, amount, max_uses, uses, expires, active, created from promos order by created desc limit 30"
    ).fetchall()
    return [
        {"code": r[0], "amount": r[1], "max_uses": r[2], "uses": r[3], "expires": r[4], "active": bool(r[5]), "created": r[6]}
        for r in rows
    ]


async def handle_admin_promo_create(request):
    if not require_admin(request):
        return jerr("Нет доступа", 403)
    d = await read_json(request)
    code = str(d.get("code", "")).strip().upper() or secrets.token_hex(4).upper()
    amount = to_int(d.get("amount"))
    max_uses = to_int(d.get("max_uses")) or 0
    days = to_int(d.get("days")) or 0
    if not re.fullmatch(r"[A-Z0-9_-]{3,32}", code):
        return jerr("Код: латиница, цифры, _ и -, от 3 до 32 символов")
    if amount is None or not 1 <= amount <= 1_000_000:
        return jerr("Сумма от 1 до 1 000 000")
    if not 0 <= max_uses <= 1_000_000 or not 0 <= days <= 3650:
        return jerr("Неверное число активаций или дней")
    now = int(time.time())
    try:
        db.execute(
            "insert into promos(code, amount, max_uses, uses, expires, active, created) values(?, ?, ?, 0, ?, 1, ?)",
            (code, amount, max_uses, now + days * 86400 if days else 0, now),
        )
        db.commit()
    except sqlite3.IntegrityError:
        db.rollback()
        return jerr("Такой код уже есть")
    data = admin_state()
    data["message"] = f"Промокод {code} создан"
    return web.json_response(data)


async def handle_admin_promo_toggle(request):
    if not require_admin(request):
        return jerr("Нет доступа", 403)
    d = await read_json(request)
    code = str(d.get("code", "")).strip().upper()
    cur = db.execute("update promos set active = 1 - active where code = ?", (code,))
    db.commit()
    if cur.rowcount != 1:
        return jerr("Промокод не найден")
    data = admin_state()
    data["message"] = "Готово"
    return web.json_response(data)


# ---------- Честность: сиды и история ставок ----------

def seed_info(uid):
    seed_id, _, server_hash, client_seed, nonce = active_seed(uid)
    return {"hash": server_hash, "client_seed": client_seed, "nonce": nonce}


async def handle_fair(request):
    user = require(request)
    if not user:
        return jerr("auth", 401)
    return web.json_response({"ok": True, "active": seed_info(user["id"])})


async def handle_fair_seed(request):
    user = require(request)
    if not user:
        return jerr("auth", 401)
    uid = user["id"]
    d = await read_json(request)
    client_seed = str(d.get("client_seed", "")).strip() or secrets.token_hex(8)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", client_seed):
        return jerr("Клиент-сид: латиница, цифры, _ и -, до 64 символов")
    if uid in active_mines:
        return jerr("Сначала закончи игру в Минах")
    seed_id, server_seed, server_hash, old_client, nonce = active_seed(uid)
    db.execute("update user_seeds set active = 0, revealed = ? where id = ?", (int(time.time()), seed_id))
    db.commit()
    create_seed(uid, client_seed)
    return web.json_response({
        "ok": True,
        "previous": {"server_seed": server_seed, "hash": server_hash, "client_seed": old_client, "nonce": nonce},
        "active": seed_info(uid),
    })


async def handle_history(request):
    user = require(request)
    if not user:
        return jerr("auth", 401)
    uid = user["id"]
    rows = db.execute(
        "select id, game, stake, payout, created, fair from bets where user_id = ? order by id desc limit 50", (uid,)
    ).fetchall()
    seed_ids, round_ids = set(), set()
    bets = []
    for bid, game, stake, payout, created, fair in rows:
        fair = json.loads(fair) if fair else None
        if fair and "seed_id" in fair:
            seed_ids.add(fair["seed_id"])
        if fair and "round_id" in fair:
            round_ids.add(fair["round_id"])
        bets.append({"id": bid, "game": game, "stake": stake, "payout": payout, "created": created, "fair": fair})
    seeds = {}
    for sid in seed_ids:
        row = db.execute("select server_seed, revealed from user_seeds where id = ? and user_id = ?", (sid, uid)).fetchone()
        if row and row[1]:
            seeds[sid] = row[0]
    rounds = {}
    for rid in round_ids:
        row = db.execute("select game, seed, hash, result, revealed from rounds where id = ?", (rid,)).fetchone()
        if row:
            rounds[rid] = {
                "game": row[0], "hash": row[2],
                "seed": row[1] if row[4] else None,
                "result": json.loads(row[3]) if row[4] and row[3] else None,
            }
    for b in bets:
        f = b["fair"]
        if not f:
            continue
        if "seed_id" in f:
            f["server_seed"] = seeds.get(f["seed_id"])
        if "round_id" in f:
            r = rounds.get(f["round_id"])
            res = r and r["result"]
            if r and res and "players" in res:
                # Чужие Telegram ID не показываем: для проверки хватает сумм по порядку
                ids = [p[0] for p in res["players"]]
                r = {**r, "result": {
                    "amounts": [p[1] for p in res["players"]], "pick": res["pick"], "fee": res.get("fee", 0),
                    "winner_index": ids.index(res["winner"]), "my_index": ids.index(uid) if uid in ids else -1,
                }}
            f["round"] = r
    return web.json_response({"ok": True, "bets": bets, "active": seed_info(uid)})


async def handle_index(request):
    return web.FileResponse(BASE_DIR / "index.html")

def valid_topup(payload, currency, amount, uid):
    # Зачисляем только звёзды по счёту, который бот сам выставил этому игроку на эту сумму
    parts = str(payload).split(":")
    return (
        currency == "XTR"
        and len(parts) == 3 and parts[0] == "topup"
        and parts[1] == str(uid)
        and parts[2] == str(amount)
        and isinstance(amount, int) and MIN_TOPUP <= amount <= MAX_TOPUP
    )


async def process_update(session, u):
    pcq = u.get("pre_checkout_query")
    if pcq:
        ok = valid_topup(pcq.get("invoice_payload", ""), pcq.get("currency"), pcq.get("total_amount"), (pcq.get("from") or {}).get("id"))
        params = {"pre_checkout_query_id": pcq["id"], "ok": ok}
        if not ok:
            params["error_message"] = "Счёт недействителен, создай новый в игре"
        await tg(session, "answerPreCheckoutQuery", **params)
        return
    cb = u.get("callback_query")
    if cb:
        remember_name(cb.get("from"))
        data = cb.get("data", "")
        parts = data.split(":")
        if is_admin(cb["from"]["id"]) and len(parts) == 3 and parts[0] == "wd" and parts[2].isdigit():
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
        if not valid_topup(payment.get("invoice_payload", ""), payment.get("currency"), amount, uid):
            print("Отклонён подозрительный платёж:", uid, payment.get("currency"), amount, payment.get("invoice_payload"))
            return
        charge_id = payment.get("telegram_payment_charge_id") or payment.get("provider_payment_charge_id")
        new_balance = credit(uid, amount, charge_id)
        if new_balance is not None:
            await tg(session, "sendMessage", chat_id=chat_id, text=f"Баланс пополнен: +{amount} звёзд\nНовый баланс: {new_balance}")
        return
    if msg.get("text", "").split("@")[0].strip().lower() == "/admin":
        sender = msg.get("from") or {}
        if is_admin(sender.get("id")) and WEBAPP_URL:
            url = f"{WEBAPP_URL}?s={make_session(sender['id'])}&admin=1"
            await tg(session, "sendMessage", chat_id=chat_id, text="Админ-панель JSQUP",
                     reply_markup={"inline_keyboard": [[{"text": "Открыть админку", "web_app": {"url": url}}]]})
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
    app["bg"] = set()
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
app.router.add_get("/avatar/{uid}", handle_avatar)
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
app.router.add_post("/api/subs/check", handle_subs_check)
app.router.add_post("/api/fair", handle_fair)
app.router.add_post("/api/fair/seed", handle_fair_seed)
app.router.add_post("/api/history", handle_history)
app.router.add_post("/api/plinko", handle_plinko)
app.router.add_post("/api/coin", handle_coin)
app.router.add_post("/api/bonus/state", handle_bonus_state)
app.router.add_post("/api/bonus/spin", handle_bonus_spin)
app.router.add_post("/api/promo", handle_promo)
app.router.add_post("/api/admin/promo/create", handle_admin_promo_create)
app.router.add_post("/api/admin/promo/toggle", handle_admin_promo_toggle)
app.router.add_post("/api/admin/state", handle_admin_state)
app.router.add_post("/api/admin/users", handle_admin_users)
app.router.add_post("/api/admin/balance", handle_admin_balance)
app.router.add_post("/api/admin/withdraw", handle_admin_withdraw)
app.router.add_post("/api/admin/settings", handle_admin_settings)
app.router.add_post("/api/admin/channels/add", handle_admin_channel_add)
app.router.add_post("/api/admin/channels/remove", handle_admin_channel_remove)
app.router.add_post("/api/admin/broadcast", handle_admin_broadcast)

app.on_startup.append(on_startup)
app.on_cleanup.append(on_cleanup)

if __name__ == "__main__":
    web.run_app(app, host=HOST, port=PORT)