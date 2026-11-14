# main.py

import asyncio
import base64
import html
import io
import json
import logging
import os
import random
import re
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, MenuButtonWebApp, Message, WebAppInfo
from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler

try:
    from PIL import Image, ImageStat
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

from data_bank import (
    ADAPTIVE_MOTIVATION_PACKS,
    EQUIPMENT_NAMES,
    EVENING_SHAME_QUOTES,
    EXCUSE_RESPONSES,
    EXERCISES,
    GOAL_NAMES,
    IDLE_CHAT_QUOTES,
    INJURY_NAMES,
    LEVEL_NAMES,
    MORNING_QUOTES,
    MUSCLE_NAMES,
    REPORT_PRAISE_QUOTES,
    TIMEZONE_LABELS,
    TIMEZONE_OPTIONS,
    WEEKDAY_NAMES,
)

# ==================== НАСТРОЙКИ ВЛАДЕЛЬЦА ====================

BOT_TOKEN = os.getenv("BOT_TOKEN", "9294187240:AAEjife-h5vu5j2yo92k8q5J8Lk7yI-0tHk")
ADMIN_TELEGRAM_ID = int(os.getenv("ADMIN_TELEGRAM_ID", "5028559439"))
DEFAULT_TIMEZONE = "Europe/Moscow"
DB_PATH = os.getenv("DB_PATH", "coach_bot.db")

WEBAPP_PORT = int(os.getenv("PORT", os.getenv("SERVER_PORT", "8080")))
WEBAPP_PUBLIC_URL = os.getenv("WEBAPP_URL", "https://bot-1794505258-4183-bober.bothost.tech").rstrip("/")

EXERCISE_MAP = {ex["id"]: ex for ex in EXERCISES}
ACTIVE_TIMERS: dict[int, asyncio.Task] = {}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
router = Router()


# ==================== БАЗА ДАННЫХ И ФИЗИОЛОГИЯ ====================

def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS user_profile (
                user_id INTEGER PRIMARY KEY,
                gender TEXT NOT NULL,
                age INTEGER NOT NULL,
                height INTEGER NOT NULL,
                weight REAL NOT NULL,
                initial_body_weight REAL NOT NULL DEFAULT 0.0,
                goal TEXT NOT NULL,
                priority_muscles TEXT NOT NULL,
                level TEXT NOT NULL,
                equipment TEXT NOT NULL,
                training_days TEXT NOT NULL,
                duration INTEGER NOT NULL,
                injuries TEXT NOT NULL,
                timezone TEXT NOT NULL DEFAULT 'Europe/Moscow',
                personal_story TEXT NOT NULL DEFAULT '',
                body_analysis TEXT NOT NULL DEFAULT '',
                workout_counter INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS exercise_progress (
                exercise_id TEXT PRIMARY KEY,
                current_weight REAL NOT NULL DEFAULT 0.0,
                target_reps INTEGER NOT NULL,
                initial_weight REAL,
                best_weight REAL NOT NULL DEFAULT 0.0,
                fail_streak INTEGER NOT NULL DEFAULT 0,
                last_updated TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS workout_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                workout_type TEXT NOT NULL,
                details TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS active_workouts (
                user_id INTEGER PRIMARY KEY,
                date TEXT NOT NULL,
                plan_json TEXT NOT NULL
            );
            """
        )
        u_cols = {r["name"] for r in conn.execute("PRAGMA table_info(user_profile)").fetchall()}
        if "initial_body_weight" not in u_cols:
            conn.execute("ALTER TABLE user_profile ADD COLUMN initial_body_weight REAL NOT NULL DEFAULT 0.0;")
        if "timezone" not in u_cols:
            conn.execute("ALTER TABLE user_profile ADD COLUMN timezone TEXT NOT NULL DEFAULT 'Europe/Moscow';")
        if "personal_story" not in u_cols:
            conn.execute("ALTER TABLE user_profile ADD COLUMN personal_story TEXT NOT NULL DEFAULT '';")
        if "body_analysis" not in u_cols:
            conn.execute("ALTER TABLE user_profile ADD COLUMN body_analysis TEXT NOT NULL DEFAULT '';")

        p_cols = {r["name"] for r in conn.execute("PRAGMA table_info(exercise_progress)").fetchall()}
        if "fail_streak" not in p_cols:
            conn.execute("ALTER TABLE exercise_progress ADD COLUMN fail_streak INTEGER NOT NULL DEFAULT 0;")


def normalize_equipment(raw_eq) -> list[str]:
    if isinstance(raw_eq, list):
        cleaned = [str(x) for x in raw_eq if str(x) in EQUIPMENT_NAMES]
        return cleaned or ["bodyweight"]
    if isinstance(raw_eq, str):
        s = raw_eq.strip()
        if s.startswith("["):
            try:
                parsed = json.loads(s)
                if isinstance(parsed, list):
                    cleaned = [str(x) for x in parsed if str(x) in EQUIPMENT_NAMES]
                    return cleaned or ["bodyweight"]
            except Exception:
                pass
        if s in EQUIPMENT_NAMES:
            return [s]
    return ["bodyweight"]


def get_user_tz(profile: dict | None = None) -> ZoneInfo:
    tz_name = profile.get("timezone", DEFAULT_TIMEZONE) if profile else DEFAULT_TIMEZONE
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo(DEFAULT_TIMEZONE)


def today_str(profile: dict | None = None) -> str:
    return datetime.now(get_user_tz(profile)).strftime("%Y-%m-%d")


def now_iso(profile: dict | None = None) -> str:
    return datetime.now(get_user_tz(profile)).isoformat()


# ==================== АНАЛИЗАТОР ФОРМЫ ТЕЛА ПО ФОТО (CV + АНТРОПОМЕТРИЯ) ====================

def _extract_image_metrics(img_bytes: bytes) -> dict:
    """Извлекает пропорции силуэта, ширину плеч/талии/бедер и рельефность (контраст теней) из фото."""
    if not HAS_PIL:
        return {"contrast": 48.0, "shoulder_waist_ratio": 1.32, "upper_lower_ratio": 1.05}
    try:
        im = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        im.thumbnail((240, 320))
        w, h = im.size
        gray = im.convert("L")

        # Центральная область тела (20%..80% ширины, 15%..88% высоты)
        torso_crop = gray.crop((int(w * 0.22), int(h * 0.18), int(w * 0.78), int(h * 0.62)))
        stat = ImageStat.Stat(torso_crop)
        contrast = float(stat.stddev[0])  # Выше стандартное отклонение яркости = глубже мышечные тени/сепарация

        # Оценка ширины силуэта по горизонтальным поясам (плечи, талия, бедра) через градиент краев
        px = gray.load()
        row_widths = []
        for y in range(int(h * 0.15), int(h * 0.88)):
            bg_l = px[2, y]
            bg_r = px[w - 3, y]
            left_x, right_x = int(w * 0.25), int(w * 0.75)
            for x in range(4, w // 2):
                if abs(px[x, y] - bg_l) > 26:
                    left_x = x
                    break
            for x in range(w - 5, w // 2, -1):
                if abs(px[x, y] - bg_r) > 26:
                    right_x = x
                    break
            span = max(10, right_x - left_x)
            row_widths.append((y / h, span))

        def avg_span(y1: float, y2: float) -> float:
            vals = [s for yr, s in row_widths if y1 <= yr <= y2]
            return sum(vals) / len(vals) if vals else 50.0

        shoulders_w = avg_span(0.20, 0.30)
        waist_w = avg_span(0.42, 0.52)
        legs_w = avg_span(0.58, 0.75)

        sw_ratio = max(1.05, min(1.75, shoulders_w / max(1.0, waist_w)))
        ul_ratio = max(0.75, min(1.45, shoulders_w / max(1.0, legs_w)))
        return {
            "contrast": round(contrast, 1),
            "shoulder_waist_ratio": round(sw_ratio, 2),
            "upper_lower_ratio": round(ul_ratio, 2),
        }
    except Exception as exc:
        logging.warning(f"CV image parse fallback: {exc}")
        return {"contrast": 48.0, "shoulder_waist_ratio": 1.32, "upper_lower_ratio": 1.05}


def analyze_body_photos(photo_bytes_list: list[bytes], profile: dict) -> dict:
    """
    Комплексный тренерский разбор формы по 1–4 фотографиям (спереди, сбоку, со спины, ноги)
    с учетом роста, веса, пола, возраста и цели атлета.
    """
    w = float(profile.get("weight", 75))
    h_cm = int(profile.get("height", 178))
    h_m = max(1.2, h_cm / 100.0)
    age = int(profile.get("age", 24))
    is_male = profile.get("gender", "Мужской") == "Мужской"
    bmi = w / (h_m * h_m)

    metrics = [_extract_image_metrics(b) for b in photo_bytes_list if b]
    avg_contrast = sum(m["contrast"] for m in metrics) / len(metrics) if metrics else 48.0
    avg_sw = sum(m["shoulder_waist_ratio"] for m in metrics) / len(metrics) if metrics else 1.32
    avg_ul = sum(m["upper_lower_ratio"] for m in metrics) / len(metrics) if metrics else 1.05

    # Оценка процента подкожного жира по формуле Деуренберга + коррекция по визуальному рельефу и V-индексу
    sex_bit = 1 if is_male else 0
    raw_bf = (1.20 * bmi) + (0.23 * age) - (10.8 * sex_bit) - 5.4
    relief_adj = (48.0 - avg_contrast) * 0.14  # Хорошая мышечная сепарация снижает оценку жира
    v_taper_adj = (1.32 - avg_sw) * 8.0
    est_bf = round(max(6.5 if is_male else 13.0, min(38.0, raw_bf + relief_adj + v_taper_adj)), 1)

    # Определение соматотипа и текущей кондиции
    if is_male:
        if est_bf <= 12.5:
            condition = "Сухая атлетичная форма (высокая детализация мышц)"
            kcal_factor = 1.15
        elif est_bf <= 17.5:
            condition = "Сбалансированная спортивная форма (оптимально для чистого набора)"
            kcal_factor = 1.08
        elif est_bf <= 22.5:
            condition = "Умеренный процент жира (нужна рекомпозиция: рост мышц + контроль талии)"
            kcal_factor = 0.96
        else:
            condition = "Повышенный процент жира (в приоритете сушка и плотный метаболический тренинг)"
            kcal_factor = 0.85
    else:
        if est_bf <= 19.0:
            condition = "Подтянутая рельефная форма"
            kcal_factor = 1.12
        elif est_bf <= 25.0:
            condition = "Сбалансированная форма (идеально для тонуса и пропорций)"
            kcal_factor = 1.04
        else:
            condition = "Повышенный процент жира (акцент на жиросжигание и сохранение мышц)"
            kcal_factor = 0.86

    # Выявление отстающих мышечных групп по пропорциям фото и ИМТ
    lagging_muscles = []
    notes = []

    if avg_sw < 1.28:
        lagging_muscles.extend(["shoulders", "back"])
        notes.append("• <b>V-силуэт (плечи и широчайшие):</b> нужно расширить верх спины и средние дельты, чтобы визуально сузить талию.")
    else:
        notes.append("• <b>Верхний плечевой пояс:</b> хорошая ширина костяка — делаем акцент на наполненность грудных и дельт.")

    if avg_ul > 1.08 or bmi < 21.5:
        lagging_muscles.extend(["quads", "hamstrings"])
        notes.append("• <b>Ноги и задняя цепь:</b> нижняя часть тела уступает верху — добавляем прицельный объем на квадрицепсы и бицепс бедра.")
    else:
        notes.append("• <b>Баланс верх/низ:</b> пропорции ног и корпуса гармоничные, сохраняем силовой прогресс в базе.")

    if bmi < 20.5:
        lagging_muscles.append("chest")
        notes.append("• <b>Общая мышечная масса:</b> дефицит мышечного каркаса — фокус на тяжелые базовые жимы и тяги в 6–10 повторениях.")
    elif est_bf >= (19.0 if is_male else 26.0):
        lagging_muscles.append("abs")
        notes.append("• <b>Кор и талия:</b> добавляем регулярную силовую работу на пресс и сокращаем паузы отдыха для расхода калорий.")

    # Убираем дубли, оставляем до 3 ключевых зон
    unique_lagging = []
    for m in lagging_muscles:
        if m not in unique_lagging:
            unique_lagging.append(m)
    unique_lagging = unique_lagging[:3] or ["chest", "back"]

    lagging_names = ", ".join(MUSCLE_NAMES[m] for m in unique_lagging)
    coach_verdict = (
        f"📸 <b>ВЕРДИКТ ТРЕНЕРА ПО ФОТО ({len(photo_bytes_list)} ракурс.):</b>\n"
        f"• Текущая кондиция: <b>{condition}</b>\n"
        f"• Оценка жировой прослойки: <b>~{est_bf}%</b> (ИМТ: {bmi:.1f})\n"
        f"• Приоритет коррекции по фото: <b>{lagging_names}</b>\n"
        + "\n".join(notes)
    )

    return {
        "photos_count": len(photo_bytes_list),
        "est_bf": est_bf,
        "bmi": round(bmi, 1),
        "condition": condition,
        "lagging_muscles": unique_lagging,
        "kcal_factor": kcal_factor,
        "verdict_html": coach_verdict,
        "updated_at": now_iso(profile),
    }


def save_body_analysis(user_id: int, analysis: dict) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE user_profile SET body_analysis = ? WHERE user_id = ?",
            (json.dumps(analysis, ensure_ascii=False), user_id),
        )
        # Сбрасываем активный план на сегодня, чтобы программа мгновенно перестроилась под фото!
        conn.execute("DELETE FROM active_workouts WHERE user_id = ?", (user_id,))


# ==================== АДАПТИВНЫЙ АНАЛИЗАТОР ТЕКСТА И МОТИВАЦИИ ====================

def detect_motivation_archetype(story: str) -> str:
    s = story.lower()

    heartbreak_kw = (
        "бывш", "девушк", "жен", "бросил", "ушла", "ушел", "расстал", "разбил",
        "предал", "измен", "люб", "отношен", "потерял ее", "потерять ее", "пожалел", "кусала локти",
    )
    prove_kw = (
        "доказат", "утереть", "нос", "не вер", "смеял", "сомневал", "враг", "соперник",
        "конкурент", "завист", "говорил", "слабак", "унизил", "лучше всех", "переплюн", "заткн",
    )
    duty_kw = (
        "семь", "мать", "мам", "отц", "пап", "родит", "сын", "доч", "дет", "брат",
        "защит", "опор", "не подвест", "гордил", "пример", "близк", "родн",
    )
    self_kw = (
        "лен", "жир", "пуз", "дрыщ", "худой", "слаб", "комплекс", "стыд", "зеркал",
        "устал быть", "ненавиж", "дисциплин", "характер", "для себя", "самоуважен", "уверенн",
    )

    scores = {
        "heartbreak_revenge": sum(2 for kw in heartbreak_kw if kw in s),
        "prove_them_wrong": sum(2 for kw in prove_kw if kw in s),
        "protector_duty": sum(2 for kw in duty_kw if kw in s),
        "self_conquest": sum(2 for kw in self_kw if kw in s),
    }

    best_key, best_score = max(scores.items(), key=lambda x: x[1])
    if best_score > 0:
        return best_key
    return "hardcore_default"


def build_personal_motivation(profile: dict | None) -> str:
    story = (profile.get("personal_story") or "").strip() if profile else ""
    goal_txt = GOAL_NAMES.get(profile["goal"], "Мощная форма") if profile else "Мощная форма"
    muscles_txt = ", ".join(MUSCLE_NAMES.get(m, m) for m in profile.get("priority_muscles", [])) if profile else ""

    if not story:
        return (
            f"🔥 <b>ТВОЯ ЦЕЛЬ — {goal_txt.upper()}!</b>\n"
            f"Акцент: <b>{muscles_txt}</b>.\n\n"
            f"{random.choice(MORNING_QUOTES)}"
        )

    archetype = detect_motivation_archetype(story)
    pack = ADAPTIVE_MOTIVATION_PACKS.get(archetype, ADAPTIVE_MOTIVATION_PACKS["hardcore_default"])

    with get_conn() as conn:
        total_w = conn.execute("SELECT COUNT(*) AS cnt FROM workout_logs").fetchone()["cnt"]
        deload_cnt = conn.execute("SELECT COUNT(*) AS cnt FROM exercise_progress WHERE fail_streak >= 1").fetchone()["cnt"]

    base_msg = random.choice(pack["templates"]).format(story=html.escape(story))

    if deload_cnt > 0:
        context_line = f"⚡ <i>Внимание: в {deload_cnt} упр. был недобор повторений — сегодня закрываем долг на 100%!</i>"
    elif total_w > 0:
        context_line = f"🏆 <i>За плечами уже {total_w} закрытых тренировок — не смей сбавлять обороты!</i>"
    else:
        context_line = f"📌 <i>Цель: {goal_txt} • Главный удар на: {muscles_txt}.</i>"

    return f"🧠 <b>Режим психологии: {pack['label']}</b>\n\n{base_msg}\n\n{context_line}"


def calculate_nutrition(profile: dict) -> dict:
    w = float(profile["weight"])
    h = int(profile["height"])
    age = int(profile["age"])
    is_male = profile["gender"] == "Мужской"

    bmr = (10 * w) + (6.25 * h) - (5 * age) + (5 if is_male else -161)
    days = len(profile["training_days"])
    act_mult = 1.375 if days <= 2 else (1.55 if days <= 4 else 1.725)
    tdee = bmr * act_mult

    goal = profile["goal"]
    if goal == "mass":
        kcal = round(tdee * 1.12)
        protein = round(w * 2.0)
        fat = round(w * 0.9)
    elif goal == "cut":
        kcal = round(tdee * 0.82)
        protein = round(w * 2.2)
        fat = round(w * 0.8)
    else:
        kcal = round(tdee * 1.05)
        protein = round(w * 2.0)
        fat = round(w * 1.0)

    # Тонкая калибровка калорийности по результатам фото-анализа тела (если он пройден)
    ba = profile.get("body_analysis")
    if isinstance(ba, dict) and ba.get("kcal_factor"):
        factor = float(ba["kcal_factor"])
        if goal == "mass" and factor < 1.0:
            # Если выбран набор массы, но по фото виден повышенный жир — ставим чистую рекомпозицию без заливания жиром
            kcal = round(tdee * 0.98)
            protein = round(w * 2.2)
        elif goal == "cut" and factor > 1.10:
            # Если человек уже очень сухой, делаем мягкий дефицит чтобы не сжечь мышцы
            kcal = round(tdee * 0.90)

    carbs = max(60, round((kcal - (protein * 4 + fat * 9)) / 4))
    return {"kcal": kcal, "protein": protein, "fat": fat, "carbs": carbs}


def save_profile(user_id: int, data: dict) -> None:
    existing = get_profile(user_id)
    counter = existing["workout_counter"] if existing else 0
    w = float(data["weight"])
    init_bw = existing["initial_body_weight"] if (existing and existing.get("initial_body_weight", 0) > 0) else w
    tz = data.get("timezone", DEFAULT_TIMEZONE)
    eq_list = normalize_equipment(data.get("equipment"))
    story = str(data.get("personal_story", existing.get("personal_story", "") if existing else "")).strip()
    existing_ba = json.dumps(existing["body_analysis"], ensure_ascii=False) if (existing and existing.get("body_analysis")) else ""

    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO user_profile (
                user_id, gender, age, height, weight, initial_body_weight, goal,
                priority_muscles, level, equipment, training_days,
                duration, injuries, timezone, personal_story, body_analysis, workout_counter, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                gender=excluded.gender,
                age=excluded.age,
                height=excluded.height,
                weight=excluded.weight,
                goal=excluded.goal,
                priority_muscles=excluded.priority_muscles,
                level=excluded.level,
                equipment=excluded.equipment,
                training_days=excluded.training_days,
                duration=excluded.duration,
                injuries=excluded.injuries,
                timezone=excluded.timezone,
                personal_story=excluded.personal_story,
                updated_at=excluded.updated_at
            """,
            (
                user_id, data["gender"], int(data["age"]), int(data["height"]), w, init_bw,
                data["goal"], json.dumps(data["priority_muscles"], ensure_ascii=False),
                data["level"], json.dumps(eq_list, ensure_ascii=False),
                json.dumps(sorted(data["training_days"])),
                int(data["duration"]), json.dumps(data["injuries"], ensure_ascii=False),
                tz, story, existing_ba, counter, now_iso({"timezone": tz}),
            ),
        )
        conn.execute("DELETE FROM active_workouts WHERE user_id = ?", (user_id,))


def update_body_weight(user_id: int, new_weight: float) -> tuple[float, float]:
    profile = get_profile(user_id)
    init_bw = profile["initial_body_weight"] if (profile and profile["initial_body_weight"] > 0) else new_weight
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE user_profile
            SET weight = ?, initial_body_weight = CASE WHEN initial_body_weight <= 0 THEN ? ELSE initial_body_weight END, updated_at = ?
            WHERE user_id = ?
            """,
            (new_weight, init_bw, now_iso(profile), user_id),
        )
    return init_bw, new_weight


def get_profile(user_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM user_profile WHERE user_id = ?", (user_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["priority_muscles"] = json.loads(d["priority_muscles"])
    d["training_days"] = json.loads(d["training_days"])
    d["injuries"] = json.loads(d["injuries"])
    d["equipment"] = normalize_equipment(d["equipment"])
    d["personal_story"] = d.get("personal_story") or ""
    raw_ba = d.get("body_analysis") or ""
    if raw_ba:
        try:
            d["body_analysis"] = json.loads(raw_ba)
        except Exception:
            d["body_analysis"] = None
    else:
        d["body_analysis"] = None
    d["motivation_archetype_label"] = (
        ADAPTIVE_MOTIVATION_PACKS[detect_motivation_archetype(d["personal_story"])]["label"]
        if d["personal_story"].strip() else ""
    )
    return d


def increment_workout_counter(user_id: int) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE user_profile SET workout_counter = workout_counter + 1 WHERE user_id = ?", (user_id,))


def has_logged_today(profile: dict | None = None) -> bool:
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM workout_logs WHERE date = ? LIMIT 1", (today_str(profile),)).fetchone()
    return row is not None


def save_workout_log(workout_type: str, logged_items: list[dict], profile: dict | None = None) -> None:
    t_str = today_str(profile)
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM workout_logs WHERE date = ? AND workout_type = ? LIMIT 1",
            (t_str, workout_type),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE workout_logs SET details = ?, created_at = ? WHERE id = ?",
                (json.dumps(logged_items, ensure_ascii=False), now_iso(profile), existing["id"]),
            )
        else:
            conn.execute(
                "INSERT INTO workout_logs (date, workout_type, details, created_at) VALUES (?, ?, ?, ?)",
                (t_str, workout_type, json.dumps(logged_items, ensure_ascii=False), now_iso(profile)),
            )


def determine_split(profile: dict) -> tuple[str, list[dict]]:
    days_count = len(profile["training_days"])

    if days_count <= 3:
        return f"Full Body Pro (Все тело, {days_count} дн./нед.)", [
            {"day_title": "Full Body — День А (Тяжелый базовый жим, присед и тяга)", "muscles": ["quads", "chest", "back", "shoulders", "biceps", "triceps", "abs"]},
            {"day_title": "Full Body — День Б (Задняя цепь, верх груди и ширина спины)", "muscles": ["hamstrings", "glutes", "back", "chest", "shoulders", "calves", "abs"]},
            {"day_title": "Full Body — День В (Гипертрофия дельт, рук и функциональный кор)", "muscles": ["chest", "back", "quads", "shoulders", "triceps", "biceps", "abs"]},
        ]

    if days_count == 4:
        return "Верх / Низ Pro (Upper / Lower, 4 дн./нед.)", [
            {"day_title": "Верх А (Силовой акцент: Грудь + Широчайшие + Передняя/средняя дельта)", "muscles": ["chest", "back", "shoulders", "biceps", "triceps"]},
            {"day_title": "Низ А (Силовой присед: Квадрицепсы + Бицепс бедра + Икры + Пресс)", "muscles": ["quads", "hamstrings", "glutes", "calves", "abs"]},
            {"day_title": "Верх Б (Гипертрофия и детализация: Толщина спины + Верх груди + Руки)", "muscles": ["back", "chest", "shoulders", "triceps", "biceps"]},
            {"day_title": "Низ Б (Тазово-доминантный: Ягодицы + Задняя цепь + Квадрицепсы + Кор)", "muscles": ["glutes", "hamstrings", "quads", "calves", "abs"]},
        ]

    return f"Тяни / Толкай / Ноги Pro (Push / Pull / Legs, {days_count} дн./нед.)", [
        {"day_title": "Толкай (Push: Грудь + Передняя/Средняя дельта + Трицепс)", "muscles": ["chest", "shoulders", "triceps", "abs"]},
        {"day_title": "Тяни (Pull: Широчайшие + Верх спины + Задняя дельта + Бицепс)", "muscles": ["back", "biceps", "shoulders"]},
        {"day_title": "Ноги (Legs: Квадрицепсы + Бицепс бедра + Ягодицы + Икры + Пресс)", "muscles": ["quads", "hamstrings", "glutes", "calves", "abs"]},
    ]


def filter_exercises(profile: dict, muscle: str) -> list[dict]:
    equip_set = set(normalize_equipment(profile["equipment"]))
    injuries = set(profile["injuries"])

    valid = [
        ex for ex in EXERCISES
        if ex["muscle"] == muscle and equip_set.intersection(ex["equip"]) and not injuries.intersection(ex["exclude_injuries"])
    ]
    if not valid:
        valid = [ex for ex in EXERCISES if ex["muscle"] == muscle and "bodyweight" in ex["equip"] and not injuries.intersection(ex["exclude_injuries"])]
    if not valid:
        valid = [ex for ex in EXERCISES if ex["muscle"] == muscle and not injuries.intersection(ex["exclude_injuries"])]
    if not valid:
        valid = [ex for ex in EXERCISES if ex["muscle"] == muscle]
    return valid


def get_exercise_targets(ex: dict, goal: str, level: str = "intermediate") -> tuple[float, int, int, int]:
    rep_min, rep_max = ex["rep_min"], ex["rep_max"]
    is_timed = ex.get("is_timed", False)

    if not is_timed:
        if goal == "strength":
            rep_min = max(4, rep_min - 2)
            rep_max = max(6, rep_max - 3)
        elif goal == "cut":
            rep_min = rep_min + 2
            rep_max = rep_max + 3

        if level == "advanced":
            rep_min += 1
            rep_max += 2
        elif level == "beginner":
            rep_min = max(4, rep_min - 1)

    with get_conn() as conn:
        row = conn.execute("SELECT current_weight, target_reps FROM exercise_progress WHERE exercise_id = ?", (ex["id"],)).fetchone()

    if row:
        weight = float(row["current_weight"])
        t_reps = max(rep_min, min(rep_max, int(row["target_reps"])))
        return weight, t_reps, rep_min, rep_max

    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO exercise_progress (exercise_id, current_weight, target_reps, initial_weight, best_weight, fail_streak, last_updated) VALUES (?, 0.0, ?, NULL, 0.0, 0, ?)",
            (ex["id"], rep_min, now_iso()),
        )
    return 0.0, rep_min, rep_min, rep_max


def get_coaching_prescription(ex: dict, is_priority: bool, goal: str, is_photo_lagging: bool = False) -> dict:
    is_comp = ex["is_compound"]
    is_timed = ex.get("is_timed", False)
    if is_timed:
        return {
            "rir": "До жжения (RPE 9)",
            "tempo": "Статика без провисания",
            "role": "📸 Коррекция по фото • Стабилизация" if is_photo_lagging else "Изометрическая стабилизация",
        }
    if is_comp:
        rir = "RIR 1–2 (1–2 повт. в запасе)" if goal != "strength" else "RIR 1 (тяжелый силовой сет)"
        tempo = "2–1–1 (2с негатив, 1с пауза)"
        if is_photo_lagging and is_priority:
            role = "🎯📸 Двойной акцент (Анкета + Анализ фото)"
        elif is_photo_lagging:
            role = "📸 Коррекция пропорций по фото"
        elif is_priority:
            role = "🎯 Ударная база (главный стимул роста)"
        else:
            role = "Базовое многосуставное движение"
    else:
        rir = "RIR 0–1 (почти до отказа)"
        tempo = "2–0–1 + 1с пиковое сжатие"
        if is_photo_lagging:
            role = "📸 Прицельная доработка по фото"
        elif is_priority:
            role = "🎯 Прицельная гипертрофия"
        else:
            role = "Изолирующая добивка и памп"
    return {"rir": rir, "tempo": tempo, "role": role}


def build_regular_workout(profile: dict) -> dict:
    split_name, split_days = determine_split(profile)
    w_counter = int(profile.get("workout_counter", 0))
    day_idx = w_counter % len(split_days)
    rotation_cycle = w_counter // len(split_days)
    day_template = split_days[day_idx]

    # Объединяем приоритеты анкеты и отстающие зоны, выявленные на фото тела
    user_priority = set(profile["priority_muscles"])
    ba = profile.get("body_analysis")
    photo_lagging = set(ba.get("lagging_muscles", [])) if isinstance(ba, dict) else set()
    effective_priority = user_priority.union(photo_lagging)

    goal = profile["goal"]
    level = profile["level"]
    max_seconds = profile["duration"] * 60

    if level == "beginner":
        base_sets = 3
        diff_rest_mult = 1.10
    elif level == "advanced":
        base_sets = 4
        diff_rest_mult = 0.90
    else:
        base_sets = 3
        diff_rest_mult = 1.0

    # Если по фото виден повышенный процент жира, чуть ускоряем темп тренировки
    bf_rest_mult = 0.90 if (isinstance(ba, dict) and float(ba.get("est_bf", 15)) >= 20.0) else 1.0
    goal_rest_mult = 1.20 if goal == "strength" else (0.80 if goal == "cut" else 1.0)
    rest_mult = goal_rest_mult * diff_rest_mult * bf_rest_mult

    big_muscles = {"chest", "back", "quads", "hamstrings", "glutes"}
    ordered_muscles = sorted(
        day_template["muscles"],
        key=lambda m: (
            0 if (m in effective_priority and m in big_muscles) else (
                1 if m in big_muscles else (
                    2 if m in effective_priority else (4 if m == "abs" else 3)
                )
            )
        ),
    )

    selected_exercises = []
    used_ids = set()
    total_time = 240

    for muscle in ordered_muscles:
        candidates = filter_exercises(profile, muscle)
        compounds = [c for c in candidates if c["is_compound"]]
        isolations = [c for c in candidates if not c["is_compound"]]

        is_priority = muscle in effective_priority
        is_lagging = muscle in photo_lagging
        want_two = is_priority and len(candidates) >= 2 and profile["duration"] >= 45 and muscle in big_muscles

        chosen_for_muscle = []
        if want_two:
            first_pool = compounds if compounds else candidates
            ex1 = first_pool[(rotation_cycle + day_idx) % len(first_pool)]
            chosen_for_muscle.append(ex1)
            second_pool = [c for c in (isolations if isolations else candidates) if c["id"] != ex1["id"]]
            if second_pool:
                ex2 = second_pool[(rotation_cycle + day_idx) % len(second_pool)]
                chosen_for_muscle.append(ex2)
        else:
            pool = compounds if (compounds and muscle in big_muscles) else candidates
            ex1 = pool[(rotation_cycle + day_idx) % len(pool)]
            chosen_for_muscle.append(ex1)

        for idx_in_m, ex in enumerate(chosen_for_muscle):
            if ex["id"] in used_ids:
                continue

            sets = base_sets + (1 if (is_priority and idx_in_m == 0) else 0)
            sets = min(4 if level != "advanced" else 5, sets)

            rest_sec = max(30, int(round((ex["rest_sec"] * rest_mult) / 5.0) * 5))
            est_ex_time = sets * (40 + rest_sec) + 50

            if total_time + est_ex_time > max_seconds and len(selected_exercises) >= 4:
                continue

            weight, t_reps, r_min, r_max = get_exercise_targets(ex, goal, level)
            rx = get_coaching_prescription(ex, muscle in user_priority, goal, is_photo_lagging=is_lagging)
            used_ids.add(ex["id"])
            selected_exercises.append({
                "id": ex["id"],
                "name": ex["name"],
                "muscle": muscle,
                "is_priority": is_priority,
                "is_photo_lagging": is_lagging,
                "is_compound": ex["is_compound"],
                "is_timed": ex.get("is_timed", False),
                "sets": sets,
                "target_weight": weight,
                "target_reps": t_reps,
                "rep_min": r_min,
                "rep_max": r_max,
                "rest_sec": rest_sec,
                "cues": ex["cues"],
                "rir": rx["rir"],
                "tempo": rx["tempo"],
                "role": rx["role"],
            })
            total_time += est_ex_time

    selected_exercises.sort(
        key=lambda x: (
            2 if x["muscle"] == "abs" else (0 if x["is_compound"] else 1),
            0 if x["is_priority"] else 1,
        )
    )

    coach_note = ""
    if isinstance(ba, dict) and ba.get("lagging_muscles"):
        l_names = ", ".join(MUSCLE_NAMES[m] for m in ba["lagging_muscles"] if m in MUSCLE_NAMES)
        coach_note = f"📸 Учтён анализ фото (~{ba['est_bf']}% жира): добавлен приоритетный объём на {l_names}."

    return {
        "workout_type": "regular",
        "split_name": split_name,
        "day_title": day_template["day_title"],
        "est_minutes": max(25, round(total_time / 60)),
        "coach_note": coach_note,
        "exercises": selected_exercises,
    }


def build_express_workout(profile: dict) -> dict:
    ba = profile.get("body_analysis")
    photo_lagging = list(ba.get("lagging_muscles", [])) if isinstance(ba, dict) else []
    priority = list(profile["priority_muscles"]) + photo_lagging
    goal = profile["goal"]
    level = profile["level"]
    target_muscles_pool = priority + ["chest", "back", "quads", "abs"]
    chosen_muscles = []
    for m in target_muscles_pool:
        if m not in chosen_muscles:
            chosen_muscles.append(m)
        if len(chosen_muscles) == 4:
            break

    sets_count = 3 if level != "beginner" else 2
    rest_val = 45 if level != "beginner" else 60

    exercises = []
    used_ids = set()
    for i, muscle in enumerate(chosen_muscles):
        candidates = filter_exercises(profile, muscle)
        candidates.sort(key=lambda x: (0 if x["is_compound"] else 1))
        ex = next((c for c in candidates if c["id"] not in used_ids), candidates[0])
        used_ids.add(ex["id"])

        weight, t_reps, r_min, r_max = get_exercise_targets(ex, goal, level)
        rx = get_coaching_prescription(ex, muscle in priority, goal, is_photo_lagging=(muscle in photo_lagging))
        superset_label = "Суперсет A" if i < 2 else "Суперсет B"
        exercises.append({
            "id": ex["id"],
            "name": f"[{superset_label}] {ex['name']}",
            "muscle": muscle,
            "is_priority": muscle in priority,
            "is_photo_lagging": muscle in photo_lagging,
            "is_compound": ex["is_compound"],
            "is_timed": ex.get("is_timed", False),
            "sets": sets_count,
            "target_weight": weight,
            "target_reps": t_reps,
            "rep_min": r_min,
            "rep_max": r_max,
            "rest_sec": rest_val,
            "cues": ex["cues"],
            "rir": rx["rir"],
            "tempo": rx["tempo"],
            "role": "⚡ Интенсивный суперсет без паузы",
        })

    return {
        "workout_type": "express",
        "split_name": "Экспресс-протокол (Антагонистические суперсеты)",
        "day_title": "Высокоплотный 20-минутный блок на целевые мышцы",
        "est_minutes": 20,
        "coach_note": "",
        "exercises": exercises,
    }


def get_or_create_today_workout(user_id: int, mode: str = "regular") -> dict:
    profile = get_profile(user_id)
    today = today_str(profile)

    with get_conn() as conn:
        row = conn.execute("SELECT date, plan_json FROM active_workouts WHERE user_id = ?", (user_id,)).fetchone()

    if row and row["date"] == today:
        saved_plan = json.loads(row["plan_json"])
        if mode == "active" or saved_plan.get("workout_type") == mode:
            return saved_plan

    plan = build_express_workout(profile) if mode == "express" else build_regular_workout(profile)
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO active_workouts (user_id, date, plan_json) VALUES (?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET date=excluded.date, plan_json=excluded.plan_json",
            (user_id, today, json.dumps(plan, ensure_ascii=False)),
        )
    return plan


def swap_exercise_in_plan(user_id: int, ex_index: int) -> tuple[bool, str, dict | None]:
    profile = get_profile(user_id)
    plan = get_or_create_today_workout(user_id, mode="active")
    exercises = plan["exercises"]
    if not (0 <= ex_index < len(exercises)):
        return False, "Упражнение не найдено.", None

    old_ex = exercises[ex_index]
    muscle = old_ex["muscle"]
    used_ids = {e["id"] for e in exercises}
    candidates = [c for c in filter_exercises(profile, muscle) if c["id"] not in used_ids]

    if not candidates:
        return False, f"Для группы «{MUSCLE_NAMES[muscle]}» под твой инвентарь и травмы других аналогов нет.", None

    new_ex = random.choice(candidates)
    weight, t_reps, r_min, r_max = get_exercise_targets(new_ex, profile["goal"], profile["level"])
    rx = get_coaching_prescription(new_ex, old_ex["is_priority"], profile["goal"], old_ex.get("is_photo_lagging", False))

    prefix_match = re.match(r"^(\[Суперсет [^\]]+\]\s*)", old_ex["name"])
    prefix = prefix_match.group(1) if prefix_match else ""

    exercises[ex_index] = {
        "id": new_ex["id"],
        "name": f"{prefix}{new_ex['name']}",
        "muscle": muscle,
        "is_priority": old_ex["is_priority"],
        "is_photo_lagging": old_ex.get("is_photo_lagging", False),
        "is_compound": new_ex["is_compound"],
        "is_timed": new_ex.get("is_timed", False),
        "sets": old_ex["sets"],
        "target_weight": weight,
        "target_reps": t_reps,
        "rep_min": r_min,
        "rep_max": r_max,
        "rest_sec": old_ex["rest_sec"],
        "cues": new_ex["cues"],
        "rir": rx["rir"],
        "tempo": rx["tempo"],
        "role": rx["role"],
    }

    with get_conn() as conn:
        conn.execute(
            "UPDATE active_workouts SET plan_json = ? WHERE user_id = ?",
            (json.dumps(plan, ensure_ascii=False), user_id),
        )
    return True, f"🔄 Заменено на: <b>{new_ex['name']}</b>", plan


def apply_double_progression(ex_item: dict, logged_weight: float, logged_reps: list[int]) -> str:
    ex_id = ex_item["id"]
    rep_min, rep_max = ex_item["rep_min"], ex_item["rep_max"]
    target_sets = ex_item["sets"]
    is_compound = ex_item["is_compound"]
    is_timed = ex_item.get("is_timed", False)
    unit = "сек" if is_timed else "повт."

    with get_conn() as conn:
        row = conn.execute("SELECT * FROM exercise_progress WHERE exercise_id = ?", (ex_id,)).fetchone()
        initial_weight = row["initial_weight"] if row and row["initial_weight"] is not None else logged_weight
        best_weight = max(float(row["best_weight"]) if row else 0.0, logged_weight)
        fail_streak = int(row["fail_streak"]) if row else 0

        hit_ceiling = len(logged_reps) >= target_sets and all(r >= rep_max for r in logged_reps[:target_sets])
        missed_floor = any(r < rep_min for r in logged_reps[:target_sets]) if logged_reps else False

        if hit_ceiling:
            fail_streak = 0
            if logged_weight > 0:
                step = 2.5 if is_compound else 1.5
                next_weight = round((logged_weight + step) * 2) / 2
                next_reps = rep_min
                msg = (
                    f"🔥 <b>ДВОЙНАЯ ПРОГРЕССИЯ!</b> Все подходы закрыты на верхнюю границу ({rep_max} {unit}).\n"
                    f"Рабочий вес повышен: <b>{logged_weight:g} кг → {next_weight:g} кг</b> (новая цель: {next_reps} {unit})."
                )
            else:
                next_weight = 0.0
                next_reps = max(logged_reps) + (5 if is_timed else 1)
                msg = f"🔥 <b>ПРОГРЕСС В СВОЕМ ВЕСЕ!</b> Новая планка на следующую тренировку: <b>{next_reps} {unit}</b>!"
        elif missed_floor and logged_weight > 0:
            fail_streak += 1
            if fail_streak >= 2:
                next_weight = max(2.5, round((logged_weight * 0.9) * 2) / 2)
                next_reps = rep_min
                fail_streak = 0
                msg = (
                    f"⚠️ <b>АВТО-РАЗГРУЗКА (DELOAD):</b> 2 тренировки подряд ниже минимума ({rep_min} {unit}).\n"
                    f"Снижаем вес на 10% для суперкомпенсации: <b>{logged_weight:g} кг → {next_weight:g} кг</b>."
                )
            else:
                next_weight = logged_weight
                next_reps = rep_min
                msg = (
                    f"⚡ Не добрал минимум ({rep_min} {unit}). Вес <b>{next_weight:g} кг</b> сохраняем — "
                    f"в следующий раз обязан закрыть {rep_min} {unit} во всех подходах!"
                )
        else:
            fail_streak = 0
            next_weight = logged_weight
            best_set_reps = max(logged_reps) if logged_reps else rep_min
            inc = 5 if is_timed else 1
            next_reps = min(rep_max, max(rep_min, best_set_reps + inc))
            w_str = f"{logged_weight:g} кг × " if logged_weight > 0 else ""
            next_w_str = f"{next_weight:g} кг × " if next_weight > 0 else ""
            msg = (
                f"✅ Записано ({w_str}{'-'.join(map(str, logged_reps))}). "
                f"Цель на следующий раз: <b>{next_w_str}{next_reps} {unit}</b>"
            )

        conn.execute(
            """
            INSERT INTO exercise_progress
            (exercise_id, current_weight, target_reps, initial_weight, best_weight, fail_streak, last_updated)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(exercise_id) DO UPDATE SET
                current_weight=excluded.current_weight,
                target_reps=excluded.target_reps,
                initial_weight=COALESCE(exercise_progress.initial_weight, excluded.initial_weight),
                best_weight=excluded.best_weight,
                fail_streak=excluded.fail_streak,
                last_updated=excluded.last_updated
            """,
            (ex_id, next_weight, next_reps, initial_weight, best_weight, fail_streak, now_iso()),
        )
    return msg


def format_profile_summary(profile: dict) -> str:
    muscles_str = ", ".join(MUSCLE_NAMES[m] for m in profile["priority_muscles"])
    days_str = ", ".join(WEEKDAY_NAMES[d] for d in sorted(profile["training_days"]))
    equip_str = ", ".join(EQUIPMENT_NAMES.get(e, e) for e in profile["equipment"])
    injuries_str = ", ".join(INJURY_NAMES[i] for i in profile["injuries"]) if profile["injuries"] else "Нет"
    tz_label = TIMEZONE_LABELS.get(profile.get("timezone", DEFAULT_TIMEZONE), profile.get("timezone", DEFAULT_TIMEZONE))
    split_name, _ = determine_split(profile)
    nut = calculate_nutrition(profile)
    story = (profile.get("personal_story") or "").strip()
    arch_lbl = profile.get("motivation_archetype_label", "")
    story_line = f"• Личный триггер: <i>«{html.escape(story)}»</i> ({arch_lbl})\n" if story else ""

    ba = profile.get("body_analysis")
    ba_block = f"\n\n{ba['verdict_html']}" if isinstance(ba, dict) and ba.get("verdict_html") else ""

    return (
        f"📋 <b>ТВОЙ ПРОФИЛЬ АТЛЕТА</b>\n"
        f"• Параметры: {profile['gender']}, {profile['age']} лет, {profile['height']} см, {profile['weight']:g} кг\n"
        f"• Главная цель: <b>{GOAL_NAMES[profile['goal']]}</b>\n"
        f"• Приоритетные мышцы: <b>{muscles_str}</b>\n"
        f"• Сложность: {LEVEL_NAMES.get(profile['level'], profile['level'])} | Инвентарь: {equip_str}\n"
        f"• График: {days_str} (~{profile['duration']} мин) | Пояс: {tz_label}\n"
        f"• Ограничения/травмы: {injuries_str}\n"
        f"{story_line}"
        f"• Подобранный сплит: <b>{split_name}</b>\n\n"
        f"🥩 <b>СУТОЧНАЯ НОРМА КБЖУ ПОД ЦЕЛЬ:</b>\n"
        f"<b>{nut['kcal']} ккал</b> | Белки: <b>{nut['protein']}г</b> | Жиры: <b>{nut['fat']}г</b> | Углеводы: <b>{nut['carbs']}г</b>"
        f"{ba_block}"
    )


def format_workout_plan(plan: dict) -> str:
    lines = [
        f"🏋️ <b>{plan['day_title']}</b>",
        f"📌 Сплит: <i>{plan['split_name']}</i>",
        f"⏱ Расчетное время: ~{plan['est_minutes']} мин",
    ]
    if plan.get("coach_note"):
        lines.append(f"<i>{plan['coach_note']}</i>")
    lines.append("")

    if plan["workout_type"] == "express":
        lines.append(
            "⚡ <b>РЕЖИМ СУПЕРСЕТОВ:</b> Выполняй упражнения внутри одного суперсета подряд без отдыха, "
            "после пары упражнений отдых 45 сек.\n"
        )

    for idx, ex in enumerate(plan["exercises"], start=1):
        star = "📸 " if ex.get("is_photo_lagging") else ("🎯 " if ex["is_priority"] else "")
        weight_txt = f"{ex['target_weight']:g} кг" if ex["target_weight"] > 0 else "Свой вес"
        unit = "сек" if ex.get("is_timed") else "повт."
        rir_txt = ex.get("rir", "RIR 1–2")
        tempo_txt = ex.get("tempo", "2–1–1")
        lines.append(
            f"<b>{idx}. {star}{ex['name']}</b>\n"
            f"   └ План: <b>{ex['sets']} × {ex['target_reps']} {unit}</b> ({ex['rep_min']}–{ex['rep_max']}) | "
            f"Вес: <b>{weight_txt}</b> | Отдых: {ex['rest_sec']}с\n"
            f"   └ Интенсивность: <b>{rir_txt}</b> | Темп: <b>{tempo_txt}</b>\n"
            f"   └ <i>Техника: {ex['cues']}</i>"
        )
    return "\n".join(lines)


def format_progress_report(profile: dict) -> str:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM exercise_progress WHERE initial_weight IS NOT NULL ORDER BY last_updated DESC").fetchall()
        total_workouts = conn.execute("SELECT COUNT(*) AS cnt FROM workout_logs").fetchone()["cnt"]
        recent_logs = conn.execute("SELECT date, workout_type FROM workout_logs ORDER BY id DESC LIMIT 5").fetchall()

    init_bw = float(profile.get("initial_body_weight") or profile["weight"])
    curr_bw = float(profile["weight"])
    bw_diff = curr_bw - init_bw
    bw_sign = "+" if bw_diff > 0 else ""
    nut = calculate_nutrition(profile)

    header = (
        f"📈 <b>МОЙ ПРОГРЕСС И ФОРМА</b>\n\n"
        f"🏆 Закрыто тренировок: <b>{total_workouts}</b>\n"
        f"⚖️ Собственный вес: <b>{init_bw:g} кг → {curr_bw:g} кг</b> ({bw_sign}{bw_diff:g} кг)\n"
        f"🥩 Актуальная норма: <b>{nut['kcal']} ккал</b> (Б: {nut['protein']}г / Ж: {nut['fat']}г / У: {nut['carbs']}г)\n"
    )

    ba = profile.get("body_analysis")
    if isinstance(ba, dict) and ba.get("verdict_html"):
        header += f"\n{ba['verdict_html']}\n"

    if not rows:
        return header + "\nПока в журнале <b>0 выполненных тренировок</b>. Сдай первый отчет, чтобы запустить трекер весов и рекордов!"

    priority = set(profile["priority_muscles"])
    lines = [header]

    for r in rows:
        ex = EXERCISE_MAP.get(r["exercise_id"])
        if not ex:
            continue

        init_w = float(r["initial_weight"] or 0.0)
        curr_w = float(r["current_weight"] or 0.0)
        diff = curr_w - init_w
        tag = "🎯" if ex["muscle"] in priority else "🔹"
        unit = "сек" if ex.get("is_timed") else "повт."
        if curr_w > 0 or init_w > 0:
            sign = "+" if diff > 0 else ""
            est_1rm = round(curr_w * (1 + int(r["target_reps"]) / 30.0), 1)
            lines.append(
                f"{tag} <b>{ex['name']}</b>: {init_w:g} → <b>{curr_w:g} кг</b> ({sign}{diff:g} кг) | ~1ПМ: {est_1rm:g} кг"
            )
        else:
            lines.append(f"{tag} <b>{ex['name']}</b>: свой вес, цель <b>{r['target_reps']} {unit}</b>")

    if recent_logs:
        logs_str = ", ".join(f"{l['date']} ({'экспресс' if l['workout_type']=='express' else 'база'})" for l in recent_logs)
        lines.append(f"\n🗓 <b>Последние тренировки:</b> {logs_str}")

    return "\n".join(lines)


async def send_safe_html(bot: Bot, chat_id: int, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
    if len(text) <= 4000:
        await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)
        return

    chunks, current = [], ""
    for block in text.split("\n\n"):
        if len(current) + len(block) + 2 <= 4000:
            current = f"{current}\n\n{block}" if current else block
        else:
            if current:
                chunks.append(current)
            current = block
    if current:
        chunks.append(current)

    for i, chunk in enumerate(chunks):
        kb = reply_markup if i == len(chunks) - 1 else None
        await bot.send_message(chat_id=chat_id, text=chunk, reply_markup=kb)


# ==================== ЗАЩИТА: ТОЛЬКО ДЛЯ ВЛАДЕЛЬЦА ====================

class AdminOnlyMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if user is None or user.id != ADMIN_TELEGRAM_ID:
            if isinstance(event, Message):
                await event.answer("⛔ <b>Доступ запрещен.</b> Этот бот принадлежит одному владельцу.")
            elif isinstance(event, CallbackQuery):
                await event.answer("⛔ Доступ запрещен.", show_alert=True)
            return
        return await handler(event, data)


# ==================== FSM И КЛАВИАТУРЫ ====================

class Onboarding(StatesGroup):
    gender = State()
    age = State()
    height = State()
    weight = State()
    goal = State()
    priority_muscles = State()
    level = State()
    equipment = State()
    training_days = State()
    duration = State()
    injuries = State()
    timezone = State()
    personal_story = State()
    body_photos = State()


class BodyScanOnly(StatesGroup):
    collecting_photos = State()


class ReportFlow(StatesGroup):
    logging_exercise = State()


class BodyWeightFlow(StatesGroup):
    waiting_weight = State()


def get_webapp_url(req: web.Request | None = None) -> str:
    if WEBAPP_PUBLIC_URL:
        return f"{WEBAPP_PUBLIC_URL}/?uid={ADMIN_TELEGRAM_ID}"
    if req is not None:
        host = req.headers.get("X-Forwarded-Host") or req.host
        return f"https://{host}/?uid={ADMIN_TELEGRAM_ID}"
    for env_key in ("BOTHOST_DOMAIN", "DOMAIN", "APP_URL", "PUBLIC_URL", "RENDER_EXTERNAL_URL"):
        val = os.getenv(env_key, "").strip().rstrip("/")
        if val:
            if not val.startswith("http"):
                val = f"https://{val}"
            return f"{val}/?uid={ADMIN_TELEGRAM_ID}"
    return ""


def main_menu_kb(profile: dict | None = None) -> InlineKeyboardMarkup:
    if profile is None:
        profile = get_profile(ADMIN_TELEGRAM_ID)
    rows = []
    w_url = get_webapp_url()
    if w_url:
        rows.append([InlineKeyboardButton(text="📱 Открыть Mini App (Pro Тренер)", web_app=WebAppInfo(url=w_url))])
    rows.append([InlineKeyboardButton(text="🏋️ План на сегодня", callback_data="menu:plan")])
    if profile and (profile.get("personal_story") or "").strip():
        rows.append([InlineKeyboardButton(text="🔥 Мотивация (Личный разряд)", callback_data="menu:motivate")])
    rows.extend(
        [
            [InlineKeyboardButton(text="📸 Анализ формы тела по фото", callback_data="menu:bodyscan")],
            [InlineKeyboardButton(text="📝 Сдать отчет по тренировке", callback_data="menu:report")],
            [InlineKeyboardButton(text="⚡ Мало времени (экспресс на 20 мин)", callback_data="menu:express")],
            [InlineKeyboardButton(text="😩 Хочу пропустить / Нет сил", callback_data="menu:excuse")],
            [InlineKeyboardButton(text="📈 Мой прогресс и КБЖУ", callback_data="menu:progress")],
            [InlineKeyboardButton(text="⚙️ Обновить анкету / цели", callback_data="menu:reconfig")],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def plan_menu_kb() -> InlineKeyboardMarkup:
    rows = []
    w_url = get_webapp_url()
    if w_url:
        rows.append([InlineKeyboardButton(text="📱 Открыть тренировку в Mini App", web_app=WebAppInfo(url=w_url))])
    rows.extend([
        [
            InlineKeyboardButton(text="⏱ 60с", callback_data="timer:60"),
            InlineKeyboardButton(text="⏱ 90с", callback_data="timer:90"),
            InlineKeyboardButton(text="⏱ 120с", callback_data="timer:120"),
            InlineKeyboardButton(text="⏱ 180с", callback_data="timer:180"),
        ],
        [
            InlineKeyboardButton(text="🔄 Заменить упр.", callback_data="menu:swap_list"),
            InlineKeyboardButton(text="🔥 Мотивация", callback_data="menu:motivate"),
        ],
        [InlineKeyboardButton(text="📝 Сдать отчет по тренировке", callback_data="menu:report")],
        [InlineKeyboardButton(text="🔙 В главное меню", callback_data="menu:home")],
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def progress_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📸 Обновить фото-анализ формы", callback_data="menu:bodyscan")],
            [InlineKeyboardButton(text="⚖️ Записать новый вес тела", callback_data="menu:bodyweight")],
            [InlineKeyboardButton(text="🔙 В главное меню", callback_data="menu:home")],
        ]
    )


def evening_kick_kb() -> InlineKeyboardMarkup:
    rows = []
    w_url = get_webapp_url()
    if w_url:
        rows.append([InlineKeyboardButton(text="📱 Открыть Mini App и закрыть долг", web_app=WebAppInfo(url=w_url))])
    rows.extend([
        [InlineKeyboardButton(text="🏋️ Показать план на сегодня", callback_data="menu:plan")],
        [InlineKeyboardButton(text="⚡ Экспресс на 20 мин", callback_data="menu:express")],
        [InlineKeyboardButton(text="📝 Сдать отчет прямо сейчас", callback_data="menu:report")],
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_muscles_kb(selected: list[str]) -> InlineKeyboardMarkup:
    rows = []
    items = list(MUSCLE_NAMES.items())
    for i in range(0, len(items), 2):
        row = []
        for key, label in items[i : i + 2]:
            mark = "✅ " if key in selected else ""
            row.append(InlineKeyboardButton(text=f"{mark}{label}", callback_data=f"onb_muscle:{key}"))
        rows.append(row)
    rows.append([InlineKeyboardButton(text="Готово ➡️", callback_data="onb_muscle:done")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_equip_kb(selected: list[str]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"{'✅ ' if k in selected else ''}{lbl}", callback_data=f"onb_eq:{k}")]
        for k, lbl in EQUIPMENT_NAMES.items()
    ]
    rows.append([InlineKeyboardButton(text="Готово ➡️", callback_data="onb_eq:done")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_days_kb(selected: list[int]) -> InlineKeyboardMarkup:
    row1 = [InlineKeyboardButton(text=f"{'✅ ' if d in selected else ''}{WEEKDAY_NAMES[d]}", callback_data=f"onb_day:{d}") for d in range(4)]
    row2 = [InlineKeyboardButton(text=f"{'✅ ' if d in selected else ''}{WEEKDAY_NAMES[d]}", callback_data=f"onb_day:{d}") for d in range(4, 7)]
    return InlineKeyboardMarkup(inline_keyboard=[row1, row2, [InlineKeyboardButton(text="Готово ➡️", callback_data="onb_day:done")]])


def build_injuries_kb(selected: list[str]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"{'✅ ' if k in selected else ''}{lbl}", callback_data=f"onb_inj:{k}")] for k, lbl in INJURY_NAMES.items()]
    rows.append([InlineKeyboardButton(text=f"{'✅ ' if not selected else ''}Травм нет", callback_data="onb_inj:none")])
    rows.append([InlineKeyboardButton(text="Далее ➡️", callback_data="onb_inj:done")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_photo_step_kb(count: int, is_onboarding: bool) -> InlineKeyboardMarkup:
    prefix = "onb_photo" if is_onboarding else "scan_photo"
    rows = []
    if count > 0:
        rows.append([InlineKeyboardButton(text=f"✅ Готово, проанализировать ({count} фото)", callback_data=f"{prefix}:done")])
    rows.append([InlineKeyboardButton(text="⏭ Пропустить" if is_onboarding else "❌ Отмена", callback_data=f"{prefix}:skip")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ==================== АНКЕТИРОВАНИЕ (/start) ====================

async def start_onboarding(target_msg: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(Onboarding.gender)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="Мужской", callback_data="onb_gender:Мужской"),
            InlineKeyboardButton(text="Женский", callback_data="onb_gender:Женский"),
        ]]
    )
    await target_msg.answer(
        "🔥 <b>Запускаем настройку твоего персонального тренера.</b>\n"
        "Отвечай честно и без воды — от этого зависит программа, КБЖУ и прогрессия нагрузок.\n\n"
        "<b>Шаг 1/14.</b> Укажи свой пол:",
        reply_markup=kb,
    )


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    profile = get_profile(message.from_user.id)
    if profile:
        await state.clear()
        await message.answer("С возвращением в строй. Твой профиль активен.\n\n" + format_profile_summary(profile), reply_markup=main_menu_kb(profile))
    else:
        await start_onboarding(message, state)


@router.message(Command("menu"))
async def cmd_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    profile = get_profile(message.from_user.id)
    if not profile:
        await start_onboarding(message, state)
        return
    await message.answer("Главное меню тренера. Выбирай задачу:", reply_markup=main_menu_kb(profile))


@router.message(Command("domain"))
async def cmd_set_domain(message: Message) -> None:
    global WEBAPP_PUBLIC_URL
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Отправь ссылку на домен Bothost так: <code>/domain https://bot1234.bothost.tech</code>")
        return
    url = parts[1].strip().rstrip("/")
    if not url.startswith("http"):
        url = f"https://{url}"
    WEBAPP_PUBLIC_URL = url
    try:
        await message.bot.set_chat_menu_button(
            chat_id=ADMIN_TELEGRAM_ID,
            menu_button=MenuButtonWebApp(text="📱 Тренер App", web_app=WebAppInfo(url=f"{WEBAPP_PUBLIC_URL}/?uid={ADMIN_TELEGRAM_ID}")),
        )
    except Exception:
        pass
    await message.answer(
        "✅ <b>Mini App успешно привязан к кнопке меню!</b>\nОткрой приложение кнопкой ниже:",
        reply_markup=main_menu_kb(),
    )


@router.callback_query(F.data == "menu:home")
async def cb_home(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.answer()
    await call.message.answer("Главное меню тренера. Выбирай задачу:", reply_markup=main_menu_kb())


@router.callback_query(F.data == "menu:reconfig")
async def cb_reconfig(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await start_onboarding(call.message, state)


@router.callback_query(Onboarding.gender, F.data.startswith("onb_gender:"))
async def onb_gender(call: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(gender=call.data.split(":", 1)[1])
    await state.set_state(Onboarding.age)
    await call.answer()
    await call.message.answer("<b>Шаг 2/14.</b> Введи свой возраст (полных лет, например: <code>26</code>):")


@router.message(Onboarding.age)
async def onb_age(message: Message, state: FSMContext) -> None:
    if not message.text or not message.text.strip().isdigit() or not (14 <= int(message.text.strip()) <= 90):
        await message.answer("⚠️ Введи возраст целым числом от 14 до 90.")
        return
    await state.update_data(age=int(message.text.strip()))
    await state.set_state(Onboarding.height)
    await message.answer("<b>Шаг 3/14.</b> Введи свой рост в сантиметрах (например: <code>180</code>):")


@router.message(Onboarding.height)
async def onb_height(message: Message, state: FSMContext) -> None:
    if not message.text or not message.text.strip().isdigit() or not (120 <= int(message.text.strip()) <= 230):
        await message.answer("⚠️ Введи реальный рост от 120 до 230 см.")
        return
    await state.update_data(height=int(message.text.strip()))
    await state.set_state(Onboarding.weight)
    await message.answer("<b>Шаг 4/14.</b> Введи свой текущий вес в кг (например: <code>78.5</code>):")


@router.message(Onboarding.weight)
async def onb_weight(message: Message, state: FSMContext) -> None:
    try:
        weight = float((message.text or "").strip().replace(",", "."))
    except ValueError:
        weight = -1
    if not (35 <= weight <= 250):
        await message.answer("⚠️ Укажи реальный вес числом от 35 до 250 кг.")
        return
    await state.update_data(weight=weight)
    await state.set_state(Onboarding.goal)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💪 Набор массы", callback_data="onb_goal:mass")],
            [InlineKeyboardButton(text="🔥 Рельеф / сушка", callback_data="onb_goal:cut")],
            [InlineKeyboardButton(text="🏋️ Максимальная сила", callback_data="onb_goal:strength")],
        ]
    )
    await message.answer("<b>Шаг 5/14.</b> Выбери главную цель:", reply_markup=kb)


@router.callback_query(Onboarding.goal, F.data.startswith("onb_goal:"))
async def onb_goal(call: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(goal=call.data.split(":", 1)[1], priority_muscles=[])
    await state.set_state(Onboarding.priority_muscles)
    await call.answer()
    await call.message.answer(
        "<b>Шаг 6/14.</b> Выбери <b>1–3 приоритетные мышечные группы</b>, которые хочешь накачать в первую очередь:",
        reply_markup=build_muscles_kb([]),
    )


@router.callback_query(Onboarding.priority_muscles, F.data.startswith("onb_muscle:"))
async def onb_muscles(call: CallbackQuery, state: FSMContext) -> None:
    val = call.data.split(":", 1)[1]
    data = await state.get_data()
    selected: list[str] = list(data.get("priority_muscles", []))

    if val == "done":
        if not selected:
            await call.answer("⚠️ Выбери хотя бы одну группу!", show_alert=True)
            return
        await state.set_state(Onboarding.level)
        await call.answer()
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=l, callback_data=f"onb_lvl:{k}")] for k, l in LEVEL_NAMES.items()])
        await call.message.answer("<b>Шаг 7/14.</b> Выбери уровень сложности тренировок:", reply_markup=kb)
        return

    if val in selected:
        selected.remove(val)
    else:
        if len(selected) >= 3:
            await call.answer("⚠️ Максимум 3 приоритетные группы!", show_alert=True)
            return
        selected.append(val)

    await state.update_data(priority_muscles=selected)
    await call.answer()
    await call.message.edit_reply_markup(reply_markup=build_muscles_kb(selected))


@router.callback_query(Onboarding.level, F.data.startswith("onb_lvl:"))
async def onb_level(call: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(level=call.data.split(":", 1)[1], equipment=[])
    await state.set_state(Onboarding.equipment)
    await call.answer()
    await call.message.answer("<b>Шаг 8/14.</b> Выбери инвентарь / места тренировок (можно несколько):", reply_markup=build_equip_kb([]))


@router.callback_query(Onboarding.equipment, F.data.startswith("onb_eq:"))
async def onb_equipment(call: CallbackQuery, state: FSMContext) -> None:
    val = call.data.split(":", 1)[1]
    data = await state.get_data()
    selected: list[str] = list(data.get("equipment", []))

    if val == "done":
        if not selected:
            await call.answer("⚠️ Выбери хотя бы 1 вариант!", show_alert=True)
            return
        await state.update_data(training_days=[])
        await state.set_state(Onboarding.training_days)
        await call.answer()
        await call.message.answer("<b>Шаг 9/14.</b> Отметь дни недели, в которые ты ОБЯЗАН тренироваться:", reply_markup=build_days_kb([]))
        return

    if val in selected:
        selected.remove(val)
    else:
        selected.append(val)

    await state.update_data(equipment=selected)
    await call.answer()
    await call.message.edit_reply_markup(reply_markup=build_equip_kb(selected))


@router.callback_query(Onboarding.training_days, F.data.startswith("onb_day:"))
async def onb_days(call: CallbackQuery, state: FSMContext) -> None:
    val = call.data.split(":", 1)[1]
    data = await state.get_data()
    selected: list[int] = list(data.get("training_days", []))

    if val == "done":
        if not selected:
            await call.answer("⚠️ Выбери минимум 1 день!", show_alert=True)
            return
        await state.set_state(Onboarding.duration)
        await call.answer()
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="30 мин", callback_data="onb_dur:30"), InlineKeyboardButton(text="45 мин", callback_data="onb_dur:45")],
                [InlineKeyboardButton(text="60 мин", callback_data="onb_dur:60"), InlineKeyboardButton(text="90 мин", callback_data="onb_dur:90")],
            ]
        )
        await call.message.answer("<b>Шаг 10/14.</b> Длительность одной тренировки:", reply_markup=kb)
        return

    d = int(val)
    selected.remove(d) if d in selected else selected.append(d)
    await state.update_data(training_days=selected)
    await call.answer()
    await call.message.edit_reply_markup(reply_markup=build_days_kb(selected))


@router.callback_query(Onboarding.duration, F.data.startswith("onb_dur:"))
async def onb_duration(call: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(duration=int(call.data.split(":", 1)[1]), injuries=[])
    await state.set_state(Onboarding.injuries)
    await call.answer()
    await call.message.answer("<b>Шаг 11/14.</b> Есть ли травмы или проблемные зоны?", reply_markup=build_injuries_kb([]))


@router.callback_query(Onboarding.injuries, F.data.startswith("onb_inj:"))
async def onb_injuries(call: CallbackQuery, state: FSMContext) -> None:
    val = call.data.split(":", 1)[1]
    data = await state.get_data()
    selected: list[str] = list(data.get("injuries", []))

    if val == "done":
        await state.set_state(Onboarding.timezone)
        await call.answer()
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=l, callback_data=f"onb_tz:{k}")] for k, l in TIMEZONE_OPTIONS])
        await call.message.answer("<b>Шаг 12/14.</b> Выбери свой часовой пояс (для уведомлений в 09:00 и 20:00):", reply_markup=kb)
        return

    if val == "none":
        selected = []
    else:
        selected.remove(val) if val in selected else selected.append(val)

    await state.update_data(injuries=selected)
    await call.answer()
    await call.message.edit_reply_markup(reply_markup=build_injuries_kb(selected))


@router.callback_query(Onboarding.timezone, F.data.startswith("onb_tz:"))
async def onb_timezone(call: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(timezone=call.data.split(":", 1)[1])
    await state.set_state(Onboarding.personal_story)
    await call.answer()
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⏭ Пропустить этот шаг", callback_data="onb_story:skip")]]
    )
    await call.message.answer(
        "<b>Шаг 13/14 (По желанию). Личный стимул и топливо для мотивации 🔥</b>\n\n"
        "Напиши одним сообщением: <b>о себе, ради чего или кого ты пашешь, кому хочешь утереть нос или кого боишься подвести / потерять</b>.\n\n"
        "Система сама проанализирует твои слова, подберет лучший психологический рычаг и добавит кнопку <b>«🔥 Мотивация»</b>.",
        reply_markup=kb,
    )


async def prompt_onboarding_photos(target_msg: Message, state: FSMContext, story: str) -> None:
    await state.update_data(personal_story=story.strip(), photo_file_ids=[])
    await state.set_state(Onboarding.body_photos)
    await target_msg.answer(
        "<b>Шаг 14/14 (По желанию). Сканирование формы тела по фото 📸</b>\n\n"
        "Отправь <b>1–4 фотографии</b> своей текущей формы при хорошем освещении:\n"
        "1️⃣ Спереди в полный рост\n"
        "2️⃣ Сбоку (профиль осанки и талии)\n"
        "3️⃣ Со спины (ширина спины и дельт)\n"
        "4️⃣ Ноги (по желанию)\n\n"
        "Я оценю процент жира, V-пропорции и отстающие мышцы, чтобы точно откалибровать объём тренировок и КБЖУ.\n"
        "<i>Отправляй фото по одному или альбомом, а затем нажми кнопку ниже:</i>",
        reply_markup=build_photo_step_kb(0, is_onboarding=True),
    )


@router.callback_query(Onboarding.personal_story, F.data == "onb_story:skip")
async def onb_story_skip(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer("Пропущено")
    await prompt_onboarding_photos(call.message, state, "")


@router.message(Onboarding.personal_story)
async def onb_story_text(message: Message, state: FSMContext) -> None:
    story = (message.text or "").strip()
    await prompt_onboarding_photos(message, state, story)


async def _download_tg_photos(bot: Bot, file_ids: list[str]) -> list[bytes]:
    result = []
    for fid in file_ids[:4]:
        try:
            tg_file = await bot.get_file(fid)
            buf = io.BytesIO()
            await bot.download_file(tg_file.file_path, destination=buf)
            result.append(buf.getvalue())
        except Exception as e:
            logging.warning(f"Не удалось скачать фото {fid}: {e}")
    return result


@router.message(Onboarding.body_photos, F.photo)
async def onb_photo_receive(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    fids: list[str] = list(data.get("photo_file_ids", []))
    fids.append(message.photo[-1].file_id)
    fids = fids[:4]
    await state.update_data(photo_file_ids=fids)
    await message.answer(
        f"📸 Фото получено (<b>{len(fids)}/4</b>). Можешь прислать ещё ракурс или нажать «Готово»:",
        reply_markup=build_photo_step_kb(len(fids), is_onboarding=True),
    )


@router.callback_query(Onboarding.body_photos, F.data.startswith("onb_photo:"))
async def onb_photo_done(call: CallbackQuery, state: FSMContext) -> None:
    action = call.data.split(":", 1)[1]
    data = await state.get_data()
    fids: list[str] = list(data.get("photo_file_ids", []))

    save_profile(call.from_user.id, data)
    profile = get_profile(call.from_user.id)

    if action == "done" and fids:
        await call.answer("Анализирую форму тела...")
        await call.message.answer("⏳ <b>Сканирую пропорции, V-индекс и мышечный рельеф по фото...</b>")
        raw_imgs = await _download_tg_photos(call.bot, fids)
        analysis = analyze_body_photos(raw_imgs, profile)
        save_body_analysis(call.from_user.id, analysis)
        profile = get_profile(call.from_user.id)
    else:
        await call.answer("Готово!")

    await state.clear()
    await call.message.answer(
        "✅ <b>Анкета принята! Идеальный сплит, периодизация, КБЖУ и рабочие объемы рассчитаны.</b>\n\n"
        + format_profile_summary(profile)
        + "\n\nНикаких оправданий — жми <b>«План на сегодня»</b> или открывай <b>Mini App</b>!",
        reply_markup=main_menu_kb(profile),
    )


# ==================== ОТДЕЛЬНЫЙ ФОТО-АНАЛИЗ ИЗ МЕНЮ ====================

@router.callback_query(F.data == "menu:bodyscan")
async def cb_bodyscan_start(call: CallbackQuery, state: FSMContext) -> None:
    profile = get_profile(call.from_user.id)
    if not profile:
        await call.answer("Сначала заполни анкету (/start)", show_alert=True)
        return
    await state.clear()
    await state.set_state(BodyScanOnly.collecting_photos)
    await state.update_data(photo_file_ids=[])
    await call.answer()
    await call.message.answer(
        "📸 <b>АНАЛИЗ ФОРМЫ ТЕЛА ПО ФОТО</b>\n\n"
        "Пришли <b>1–4 фотографии</b> своей актуальной формы (спереди, сбоку, со спины, ноги) при ровном освещении.\n"
        "После разбора я сразу перестрою акценты и КБЖУ твоей программы под реальную картину:",
        reply_markup=build_photo_step_kb(0, is_onboarding=False),
    )


@router.message(BodyScanOnly.collecting_photos, F.photo)
async def scan_photo_receive(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    fids: list[str] = list(data.get("photo_file_ids", []))
    fids.append(message.photo[-1].file_id)
    fids = fids[:4]
    await state.update_data(photo_file_ids=fids)
    await message.answer(
        f"📸 Принято ракурсов: <b>{len(fids)}/4</b>. Отправь ещё или жми «Готово»:",
        reply_markup=build_photo_step_kb(len(fids), is_onboarding=False),
    )


@router.callback_query(BodyScanOnly.collecting_photos, F.data.startswith("scan_photo:"))
async def scan_photo_finish(call: CallbackQuery, state: FSMContext) -> None:
    action = call.data.split(":", 1)[1]
    data = await state.get_data()
    fids: list[str] = list(data.get("photo_file_ids", []))
    await state.clear()

    if action == "skip" or not fids:
        await call.answer("Отменено")
        await call.message.answer("Возвращаю в главное меню:", reply_markup=main_menu_kb())
        return

    await call.answer("Анализирую фото...")
    await call.message.answer("⏳ <b>Провожу биометрический анализ фото и перестраиваю программу...</b>")
    profile = get_profile(call.from_user.id)
    raw_imgs = await _download_tg_photos(call.bot, fids)
    analysis = analyze_body_photos(raw_imgs, profile)
    save_body_analysis(call.from_user.id, analysis)
    profile = get_profile(call.from_user.id)
    nut = calculate_nutrition(profile)

    await call.message.answer(
        f"{analysis['verdict_html']}\n\n"
        f"🥩 <b>Обновлённая норма КБЖУ:</b> <b>{nut['kcal']} ккал</b> (Б: {nut['protein']}г / Ж: {nut['fat']}г / У: {nut['carbs']}г)\n"
        f"✅ Программа на сегодня уже пересобрана с учётом этих акцентов!",
        reply_markup=main_menu_kb(profile),
    )


# ==================== ЛИЧНАЯ МОТИВАЦИЯ ====================

@router.callback_query(F.data == "menu:motivate")
async def cb_motivate(call: CallbackQuery) -> None:
    profile = get_profile(call.from_user.id)
    if not profile:
        await call.answer("Сначала заполни анкету (/start)", show_alert=True)
        return
    await call.answer("🔥 Заряд выдан!")
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔥 Ещё заряд мотивации", callback_data="menu:motivate")],
            [InlineKeyboardButton(text="🏋️ К плану на сегодня", callback_data="menu:plan_active")],
            [InlineKeyboardButton(text="🔙 В главное меню", callback_data="menu:home")],
        ]
    )
    await call.message.answer(build_personal_motivation(profile), reply_markup=kb)


# ==================== ТАЙМЕР ОТДЫХА И ЗАМЕНА УПРАЖНЕНИЯ ====================

async def _timer_worker(bot: Bot, chat_id: int, seconds: int) -> None:
    try:
        await asyncio.sleep(seconds)
        await bot.send_message(chat_id=chat_id, text=f"⏰ <b>ВРЕМЯ ОТДЫХА ВЫШЛО ({seconds} сек)!</b>\nХватит сидеть в телефоне — под снаряд и за работу!")
    except asyncio.CancelledError:
        pass
    finally:
        if ACTIVE_TIMERS.get(chat_id) is asyncio.current_task():
            ACTIVE_TIMERS.pop(chat_id, None)


@router.callback_query(F.data.startswith("timer:"))
async def cb_start_timer(call: CallbackQuery) -> None:
    sec = int(call.data.split(":", 1)[1])
    user_id = call.from_user.id
    old_task = ACTIVE_TIMERS.get(user_id)
    if old_task and not old_task.done():
        old_task.cancel()
    ACTIVE_TIMERS[user_id] = asyncio.create_task(_timer_worker(call.bot, user_id, sec))
    await call.answer(f"⏱ Таймер на {sec} сек запущен!")


@router.callback_query(F.data == "menu:swap_list")
async def cb_swap_list(call: CallbackQuery) -> None:
    profile = get_profile(call.from_user.id)
    if not profile:
        await call.answer("Сначала заполни анкету (/start)", show_alert=True)
        return
    plan = get_or_create_today_workout(call.from_user.id, mode="active")
    rows = [
        [InlineKeyboardButton(text=f"🔄 {i+1}. {ex['name'][:38]}", callback_data=f"swap_do:{i}")]
        for i, ex in enumerate(plan["exercises"])
    ]
    rows.append([InlineKeyboardButton(text="🔙 Назад к тренировке", callback_data="menu:plan_active")])
    await call.answer()
    await call.message.answer("Выбери упражнение, которое нужно заменить на аналог (если тренажер занят):", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("swap_do:"))
async def cb_swap_do(call: CallbackQuery) -> None:
    idx = int(call.data.split(":", 1)[1])
    ok, msg, plan = swap_exercise_in_plan(call.from_user.id, idx)
    if not ok or not plan:
        await call.answer(msg, show_alert=True)
        return
    await call.answer("Заменено!")
    await send_safe_html(call.bot, call.from_user.id, f"{msg}\n\n{format_workout_plan(plan)}", reply_markup=plan_menu_kb())


# ==================== ГЛАВНОЕ МЕНЮ ====================

@router.callback_query(F.data == "menu:plan_active")
async def cb_plan_active(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    plan = get_or_create_today_workout(call.from_user.id, mode="active")
    await call.answer()
    await send_safe_html(call.bot, call.from_user.id, format_workout_plan(plan), reply_markup=plan_menu_kb())


@router.callback_query(F.data == "menu:plan")
async def cb_plan(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    profile = get_profile(call.from_user.id)
    if not profile:
        await call.answer()
        await start_onboarding(call.message, state)
        return
    plan = get_or_create_today_workout(call.from_user.id, mode="regular")
    await call.answer()
    await send_safe_html(call.bot, call.from_user.id, format_workout_plan(plan), reply_markup=plan_menu_kb())


@router.callback_query(F.data == "menu:express")
async def cb_express(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    profile = get_profile(call.from_user.id)
    if not profile:
        await call.answer()
        await start_onboarding(call.message, state)
        return
    plan = get_or_create_today_workout(call.from_user.id, mode="express")
    await call.answer("Собран экспресс-протокол!")
    text = "⚡ <b>Времени мало — значит, плотность работы будет максимальной!</b>\n\n" + format_workout_plan(plan)
    await send_safe_html(call.bot, call.from_user.id, text, reply_markup=plan_menu_kb())


@router.callback_query(F.data == "menu:excuse")
async def cb_excuse(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    profile = get_profile(call.from_user.id)
    goal_line = f"Твоя цель — <b>{GOAL_NAMES[profile['goal']]}</b>. " if profile else ""
    story_line = ""
    if profile and (profile.get("personal_story") or "").strip():
        story_line = f"\n\n{build_personal_motivation(profile)}\n"
    await call.answer("Отговорки не принимаются!")
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⚡ Включить Экспресс на 20 мин", callback_data="menu:express")],
            [InlineKeyboardButton(text="🏋️ Иду делать базу по плану", callback_data="menu:plan")],
        ]
    )
    await call.message.answer(
        f"🛑 <b>СТОП. Оправдания не принимаются.</b>\n\n{goal_line}{random.choice(EXCUSE_RESPONSES)}{story_line}",
        reply_markup=kb,
    )


@router.callback_query(F.data == "menu:progress")
async def cb_progress(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    profile = get_profile(call.from_user.id)
    if not profile:
        await call.answer("Сначала заполни анкету (/start)", show_alert=True)
        return
    await call.answer()
    await send_safe_html(call.bot, call.from_user.id, format_progress_report(profile), reply_markup=progress_menu_kb())


# ==================== ОБНОВЛЕНИЕ ВЕСА ТЕЛА ====================

@router.callback_query(F.data == "menu:bodyweight")
async def cb_bodyweight_start(call: CallbackQuery, state: FSMContext) -> None:
    profile = get_profile(call.from_user.id)
    if not profile:
        await call.answer("Сначала заполни анкету (/start)", show_alert=True)
        return
    await state.set_state(BodyWeightFlow.waiting_weight)
    await call.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="menu:home")]])
    await call.message.answer(
        f"⚖️ Текущий вес: <b>{profile['weight']:g} кг</b>.\nВведи новый вес в кг (например: <code>79.2</code>):",
        reply_markup=kb,
    )


@router.message(BodyWeightFlow.waiting_weight)
async def msg_bodyweight_input(message: Message, state: FSMContext) -> None:
    try:
        w = float((message.text or "").strip().replace(",", "."))
    except ValueError:
        w = -1
    if not (35 <= w <= 250):
        await message.answer("⚠️ Введи реальный вес числом от 35 до 250 кг.")
        return
    init_bw, curr_bw = update_body_weight(message.from_user.id, w)
    profile = get_profile(message.from_user.id)
    nut = calculate_nutrition(profile)
    diff = curr_bw - init_bw
    sign = "+" if diff > 0 else ""
    await state.clear()
    await message.answer(
        f"✅ Вес обновлен: <b>{curr_bw:g} кг</b> (от старта: <b>{sign}{diff:g} кг</b>).\n"
        f"🥩 Пересчитанная норма: <b>{nut['kcal']} ккал</b> (Б: {nut['protein']}г / Ж: {nut['fat']}г / У: {nut['carbs']}г).",
        reply_markup=main_menu_kb(profile),
    )


# ==================== СДАЧА ОТЧЕТА И ДВОЙНАЯ ПРОГРЕССИЯ ====================

async def prompt_next_exercise(target_msg: Message, exercises: list[dict], idx: int) -> None:
    ex = exercises[idx]
    total = len(exercises)
    w, reps, sets = ex["target_weight"], ex["target_reps"], ex["sets"]
    unit = "сек" if ex.get("is_timed") else "повт."
    w_label = f"{w:g} кг" if w > 0 else "Свой вес (0 кг)"
    exact_reps_preview = " ".join([str(reps)] * sets)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"✅ Выполнил план ({w:g} кг × {exact_reps_preview})", callback_data="rep:exact")],
            [
                InlineKeyboardButton(text="⏱ 60с", callback_data="timer:60"),
                InlineKeyboardButton(text="⏱ 90с", callback_data="timer:90"),
                InlineKeyboardButton(text="⏱ 120с", callback_data="timer:120"),
            ],
            [
                InlineKeyboardButton(text="⏭ Пропустить", callback_data="rep:skip"),
                InlineKeyboardButton(text="❌ Отменить отчет", callback_data="menu:home"),
            ],
        ]
    )

    await target_msg.answer(
        f"📝 <b>Упражнение {idx + 1} из {total}:</b>\n"
        f"<b>{ex['name']}</b>\n"
        f"🎯 План: <b>{sets} подх. × {reps} {unit}</b> (диапазон {ex['rep_min']}–{ex['rep_max']}) | Вес: <b>{w_label}</b>\n\n"
        f"Жми кнопку ниже, если закрыл план 1-в-1, или введи факт: <code>вес повт1 повт2 ...</code> (пример: <code>{w:g} {exact_reps_preview}</code>).",
        reply_markup=kb,
    )


def parse_report_line(text: str, default_weight: float, expected_sets: int) -> tuple[float, list[int]] | None:
    tokens = [t.strip() for t in re.split(r"[\s;]+", text.strip()) if t.strip()]
    if not tokens:
        return None
    try:
        first_val = float(tokens[0].replace(",", "."))
    except ValueError:
        return None

    if default_weight == 0.0 and len(tokens) == expected_sets and first_val.is_integer():
        try:
            reps = [int(t) for t in tokens]
            if all(r > 0 for r in reps):
                return 0.0, reps
        except ValueError:
            pass

    if len(tokens) < 2 or first_val < 0:
        return None
    try:
        reps = [int(t) for t in tokens[1:]]
    except ValueError:
        return None
    if not reps or any(r <= 0 for r in reps):
        return None
    return first_val, reps


async def advance_report_step(target_msg: Message, state: FSMContext, logged_weight: float | None, logged_reps: list[int] | None) -> None:
    data = await state.get_data()
    exercises: list[dict] = data["exercises"]
    idx: int = data["idx"]
    logged_results: list[dict] = list(data["logged_results"])
    workout_type: str = data["workout_type"]
    current_ex = exercises[idx]

    if logged_weight is not None and logged_reps is not None:
        prog_feedback = apply_double_progression(current_ex, logged_weight, logged_reps)
        logged_results.append({"id": current_ex["id"], "name": current_ex["name"], "weight": logged_weight, "reps": logged_reps})
        await target_msg.answer(prog_feedback)
    else:
        await target_msg.answer(f"⏭ Упражнение <b>{current_ex['name']}</b> пропущено.")

    next_idx = idx + 1
    if next_idx < len(exercises):
        await state.update_data(idx=next_idx, logged_results=logged_results)
        await prompt_next_exercise(target_msg, exercises, next_idx)
    else:
        profile = get_profile(ADMIN_TELEGRAM_ID)
        if logged_results:
            already_done = has_logged_today(profile)
            save_workout_log(workout_type, logged_results, profile)
            if workout_type == "regular" and not already_done:
                increment_workout_counter(ADMIN_TELEGRAM_ID)
            summary = f"🏁 <b>ОТЧЕТ ПО ТРЕНИРОВКЕ ЗАКРЫТ!</b>\nВыполнено упражнений: <b>{len(logged_results)}/{len(exercises)}</b>.\n\n{random.choice(REPORT_PRAISE_QUOTES)}"
        else:
            summary = "⚠️ Ты пропустил все упражнения. Тренировка не засчитана в журнал."
        await state.clear()
        await target_msg.answer(summary, reply_markup=main_menu_kb(profile))


@router.callback_query(F.data == "menu:report")
async def cb_start_report(call: CallbackQuery, state: FSMContext) -> None:
    profile = get_profile(call.from_user.id)
    if not profile:
        await call.answer("Сначала заполни анкету (/start)", show_alert=True)
        return
    plan = get_or_create_today_workout(call.from_user.id, mode="active")
    await state.clear()
    await state.set_state(ReportFlow.logging_exercise)
    await state.update_data(exercises=plan["exercises"], idx=0, logged_results=[], workout_type=plan["workout_type"])
    await call.answer()
    await prompt_next_exercise(call.message, plan["exercises"], 0)


@router.callback_query(ReportFlow.logging_exercise, F.data == "rep:exact")
async def cb_report_exact(call: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    ex = data["exercises"][data["idx"]]
    await call.answer("Записано!")
    await advance_report_step(call.message, state, float(ex["target_weight"]), [int(ex["target_reps"])] * int(ex["sets"]))


@router.callback_query(ReportFlow.logging_exercise, F.data == "rep:skip")
async def cb_report_skip(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer("Пропущено")
    await advance_report_step(call.message, state, None, None)


@router.message(ReportFlow.logging_exercise)
async def msg_report_input(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    ex = data["exercises"][data["idx"]]
    parsed = parse_report_line(message.text or "", float(ex["target_weight"]), int(ex["sets"]))
    if not parsed:
        await message.answer("⚠️ Неверный формат. Пример: <code>60 12 12 11 10</code> (для своего веса: <code>0 15 14 12</code>).")
        return
    await advance_report_step(message, state, parsed[0], parsed[1])


@router.callback_query()
async def cb_fallback_expired(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.answer("Сессия обновилась, открываю главное меню.")
    await call.message.answer("Выбери действие в меню:", reply_markup=main_menu_kb())


@router.message()
async def msg_idle_fallback(message: Message) -> None:
    if not get_profile(message.from_user.id):
        await message.answer("Нажми /start, чтобы заполнить анкету и начать работу.")
        return
    await message.answer(f"💬 {random.choice(IDLE_CHAT_QUOTES)}", reply_markup=main_menu_kb())


# ==================== ВЕБ-СЕРВЕР MINI APP (aiohttp) ====================

async def handle_index(request: web.Request) -> web.Response:
    global WEBAPP_PUBLIC_URL
    if not WEBAPP_PUBLIC_URL and request.host and not request.host.startswith("127.") and not request.host.startswith("0.0.0.0"):
        WEBAPP_PUBLIC_URL = f"https://{request.host}"
    html_path = os.path.join(os.path.dirname(__file__) or ".", "webapp.html")
    with open(html_path, "r", encoding="utf-8") as f:
        content = f.read()
    return web.Response(
        text=content,
        content_type="text/html",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
    )


async def handle_api_state(request: web.Request) -> web.Response:
    mode = request.query.get("mode", "active")
    if mode not in ("regular", "express", "active"):
        mode = "active"
    profile = get_profile(ADMIN_TELEGRAM_ID)
    if not profile:
        return web.json_response({
            "has_profile": False,
            "muscle_names": MUSCLE_NAMES,
            "weekday_names": WEEKDAY_NAMES,
            "injury_names": INJURY_NAMES,
            "equipment_names": EQUIPMENT_NAMES,
            "level_names": LEVEL_NAMES,
        })

    plan = get_or_create_today_workout(ADMIN_TELEGRAM_ID, mode=mode)
    for ex in plan.get("exercises", []):
        if "rir" not in ex or "tempo" not in ex:
            rx = get_coaching_prescription(ex, ex.get("is_priority", False), profile["goal"], ex.get("is_photo_lagging", False))
            ex.setdefault("rir", rx["rir"])
            ex.setdefault("tempo", rx["tempo"])
            ex.setdefault("role", rx["role"])

    split_name, _ = determine_split(profile)
    nut = calculate_nutrition(profile)

    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM exercise_progress WHERE initial_weight IS NOT NULL ORDER BY last_updated DESC").fetchall()
        total_workouts = conn.execute("SELECT COUNT(*) AS cnt FROM workout_logs").fetchone()["cnt"]

    priority = set(profile["priority_muscles"])
    prog_items = []
    for r in rows:
        ex = EXERCISE_MAP.get(r["exercise_id"])
        if not ex:
            continue
        init_w = float(r["initial_weight"] or 0.0)
        curr_w = float(r["current_weight"] or 0.0)
        t_reps = int(r["target_reps"] or ex["rep_min"])
        est_1rm = round(curr_w * (1 + t_reps / 30.0), 1) if curr_w > 0 else 0.0
        prog_items.append({
            "id": ex["id"],
            "name": ex["name"],
            "is_priority": ex["muscle"] in priority,
            "is_timed": ex.get("is_timed", False),
            "target_reps": t_reps,
            "init_w": init_w,
            "curr_w": curr_w,
            "diff": round(curr_w - init_w, 1),
            "est_1rm": est_1rm,
        })

    return web.json_response({
        "has_profile": True,
        "profile": profile,
        "profile_summary": {"split_name": split_name},
        "nutrition": nut,
        "plan": plan,
        "quote": random.choice(MORNING_QUOTES),
        "personal_motivation": build_personal_motivation(profile),
        "progress": {"total_workouts": total_workouts, "items": prog_items},
        "muscle_names": MUSCLE_NAMES,
        "weekday_names": WEEKDAY_NAMES,
        "injury_names": INJURY_NAMES,
        "equipment_names": EQUIPMENT_NAMES,
        "level_names": LEVEL_NAMES,
    })


async def handle_api_log_exercise(request: web.Request) -> web.Response:
    body = await request.json()
    idx = int(body.get("idx", 0))
    weight = float(body.get("weight", 0.0))
    reps = [int(x) for x in body.get("reps", []) if int(x) > 0]

    plan = get_or_create_today_workout(ADMIN_TELEGRAM_ID, mode="active")
    exercises = plan["exercises"]
    if not (0 <= idx < len(exercises)):
        return web.json_response({"ok": False, "message": "Упражнение не найдено."})

    ex_item = exercises[idx]
    msg = apply_double_progression(ex_item, weight, reps)
    ex_item["logged_today"] = True
    ex_item["logged_weight"] = weight
    ex_item["logged_reps"] = reps

    with get_conn() as conn:
        conn.execute(
            "UPDATE active_workouts SET plan_json = ? WHERE user_id = ?",
            (json.dumps(plan, ensure_ascii=False), ADMIN_TELEGRAM_ID),
        )
    return web.json_response({"ok": True, "message": msg})


async def handle_api_swap(request: web.Request) -> web.Response:
    body = await request.json()
    idx = int(body.get("idx", 0))
    ok, msg, _ = swap_exercise_in_plan(ADMIN_TELEGRAM_ID, idx)
    return web.json_response({"ok": ok, "message": msg})


async def handle_api_finish_workout(request: web.Request) -> web.Response:
    profile = get_profile(ADMIN_TELEGRAM_ID)
    plan = get_or_create_today_workout(ADMIN_TELEGRAM_ID, mode="active")
    already_done = has_logged_today(profile)

    logged = [
        {"id": ex["id"], "name": ex["name"], "weight": ex.get("logged_weight", ex["target_weight"]), "reps": ex.get("logged_reps", [ex["target_reps"]] * ex["sets"])}
        for ex in plan["exercises"] if ex.get("logged_today")
    ]
    if not logged:
        for ex in plan["exercises"]:
            w_val = float(ex["target_weight"])
            r_list = [int(ex["target_reps"])] * int(ex["sets"])
            apply_double_progression(ex, w_val, r_list)
            ex["logged_today"] = True
            ex["logged_weight"] = w_val
            ex["logged_reps"] = r_list
            logged.append({"id": ex["id"], "name": ex["name"], "weight": w_val, "reps": r_list})
        with get_conn() as conn:
            conn.execute(
                "UPDATE active_workouts SET plan_json = ? WHERE user_id = ?",
                (json.dumps(plan, ensure_ascii=False), ADMIN_TELEGRAM_ID),
            )

    save_workout_log(plan["workout_type"], logged, profile)
    if plan["workout_type"] == "regular" and not already_done:
        increment_workout_counter(ADMIN_TELEGRAM_ID)

    praise = random.choice(REPORT_PRAISE_QUOTES)
    bot_inst: Bot | None = request.app.get("bot")
    if bot_inst:
        try:
            await bot_inst.send_message(
                chat_id=ADMIN_TELEGRAM_ID,
                text=f"🏁 <b>ТРЕНИРОВКА В MINI APP ЗАКРЫТА!</b>\nВыполнено упражнений: <b>{len(logged)}/{len(plan['exercises'])}</b>.\n\n{praise}",
                reply_markup=main_menu_kb(profile),
            )
        except Exception:
            pass
    return web.json_response({"ok": True, "message": f"🏁 Тренировка закрыта! {praise}"})


async def handle_api_excuse(request: web.Request) -> web.Response:
    profile = get_profile(ADMIN_TELEGRAM_ID)
    story = (profile.get("personal_story") or "").strip() if profile else ""
    base = random.choice(EXCUSE_RESPONSES)
    if story:
        base += f"<br><br>{build_personal_motivation(profile)}"
    return web.json_response({"ok": True, "message": base})


async def handle_api_motivate(request: web.Request) -> web.Response:
    profile = get_profile(ADMIN_TELEGRAM_ID)
    return web.json_response({"ok": True, "message": build_personal_motivation(profile)})


async def handle_api_analyze_photos(request: web.Request) -> web.Response:
    profile = get_profile(ADMIN_TELEGRAM_ID)
    if not profile:
        return web.json_response({"ok": False, "message": "Сначала сохрани базовую анкету!"})
    body = await request.json()
    b64_list = body.get("photos", [])
    raw_bytes = []
    for item in b64_list[:4]:
        s = str(item)
        if "," in s:
            s = s.split(",", 1)[1]
        try:
            raw_bytes.append(base64.b64decode(s))
        except Exception:
            pass

    if not raw_bytes:
        return web.json_response({"ok": False, "message": "Не удалось прочитать изображения."})

    analysis = analyze_body_photos(raw_bytes, profile)
    save_body_analysis(ADMIN_TELEGRAM_ID, analysis)
    return web.json_response({"ok": True, "analysis": analysis})


async def handle_api_bodyweight(request: web.Request) -> web.Response:
    body = await request.json()
    w = float(body.get("weight", 75.0))
    init_bw, curr_bw = update_body_weight(ADMIN_TELEGRAM_ID, w)
    diff = curr_bw - init_bw
    sign = "+" if diff > 0 else ""
    return web.json_response({"ok": True, "message": f"✅ Вес сохранен: {curr_bw:g} кг ({sign}{diff:g} кг от старта)"})


async def handle_api_save_profile(request: web.Request) -> web.Response:
    body = await request.json()
    save_profile(ADMIN_TELEGRAM_ID, body)
    return web.json_response({"ok": True})


# ==================== ПЛАНИРОВЩИК ПРОТИВ ЛЕНИ (APScheduler) ====================

async def hourly_discipline_check(bot: Bot) -> None:
    profile = get_profile(ADMIN_TELEGRAM_ID)
    if not profile:
        return
    now_local = datetime.now(get_user_tz(profile))
    if now_local.weekday() not in profile["training_days"]:
        return
    try:
        story = (profile.get("personal_story") or "").strip()
        if now_local.hour == 9:
            plan = get_or_create_today_workout(ADMIN_TELEGRAM_ID, mode="regular")
            mot_line = f"\n\n{build_personal_motivation(profile)}" if story else f"\n\n{random.choice(MORNING_QUOTES)}"
            text = f"⏰ <b>09:00 | ПОДЪЕМ НА ТРЕНИРОВКУ</b>{mot_line}\n\n{format_workout_plan(plan)}"
            await send_safe_html(bot, ADMIN_TELEGRAM_ID, text, reply_markup=plan_menu_kb())
        elif now_local.hour == 20 and not has_logged_today(profile):
            story_shame = f"\n\n{build_personal_motivation(profile)}" if story else ""
            text = f"🚨 <b>20:00 | КОНТРОЛЬ ДИСЦИПЛИНЫ</b>\n\n{random.choice(EVENING_SHAME_QUOTES)}{story_shame}"
            await bot.send_message(chat_id=ADMIN_TELEGRAM_ID, text=text, reply_markup=evening_kick_kb())
    except Exception as exc:
        logging.error(f"Ошибка планировщика: {exc}")


async def main() -> None:
    init_db()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.message.middleware(AdminOnlyMiddleware())
    dp.callback_query.middleware(AdminOnlyMiddleware())
    dp.include_router(router)

    app = web.Application(client_max_size=25 * 1024 * 1024)
    app["bot"] = bot
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/state", handle_api_state)
    app.router.add_post("/api/log_exercise", handle_api_log_exercise)
    app.router.add_post("/api/swap", handle_api_swap)
    app.router.add_post("/api/finish_workout", handle_api_finish_workout)
    app.router.add_get("/api/excuse", handle_api_excuse)
    app.router.add_get("/api/motivate", handle_api_motivate)
    app.router.add_post("/api/analyze_photos", handle_api_analyze_photos)
    app.router.add_post("/api/bodyweight", handle_api_bodyweight)
    app.router.add_post("/api/save_profile", handle_api_save_profile)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=WEBAPP_PORT)
    await site.start()
    logging.info(f"Mini App веб-сервер запущен на порту {WEBAPP_PORT}")

    w_url = get_webapp_url()
    if w_url:
        try:
            await bot.set_chat_menu_button(
                chat_id=ADMIN_TELEGRAM_ID,
                menu_button=MenuButtonWebApp(text="📱 Тренер App", web_app=WebAppInfo(url=w_url)),
            )
        except Exception as e:
            logging.warning(f"Не удалось установить кнопку меню: {e}")

    scheduler = AsyncIOScheduler()
    scheduler.add_job(hourly_discipline_check, "cron", minute=0, args=[bot])
    scheduler.start()

    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("Бот-тренер + Mini App запущены и следят за дисциплиной.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
