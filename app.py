# -*- coding: utf-8 -*-
"""
看護師・介護士向け 勤務表自動生成システム
========================================
Streamlit + SQLite + Google OR-Tools(CP-SAT) による、
病棟・施設向けシフト自動作成アプリです。

【重要な注意】
このアプリが自動生成するシフト表は、あくまで「たたき台」です。
最終的な勤務表としての適法性・妥当性は、必ず施設の管理者・社会保険労務士等の
専門家が確認したうえで確定させてください。
特に「連続勤務日数の上限」等、法律上の絶対ルールではなく施設ごとの
運用ルールとして扱っている項目があります（アプリ内の解説を参照）。
"""

import streamlit as st
import sqlite3
import pandas as pd
from datetime import datetime, date, timedelta
import calendar
import io
import json

# OR-Toolsは「シフト自動生成」タブでのみ使用するため、遅延インポートにして
# 万一未インストールの環境でも他のタブが動くようにしておく
try:
    from ortools.sat.python import cp_model
    ORTOOLS_AVAILABLE = True
except ImportError:
    ORTOOLS_AVAILABLE = False

DB_PATH = "shift_app.db"

# ─────────────────────────────────────────────
# シフト区分の初期定義（設定タブから編集可能）
# ─────────────────────────────────────────────
DEFAULT_SHIFT_TYPES = [
    # code, name, start, end, hours, is_night(夜勤入り扱いか), leave_amount(有給消費日数、勤務系は0)
    ("N",   "日勤",       "08:30", "17:30", 8.0,  False, 0),
    ("準",  "準夜勤",     "16:30", "01:00", 8.0,  False, 0),
    ("入",  "夜勤入り",   "16:30", "09:00", 16.0, True,  0),
    ("明",  "明け",       "", "", 0.0, False, 0),
    ("am",  "午前勤務",   "08:30", "12:30", 4.0, False, 0),
    ("pm",  "午後勤務",   "13:30", "17:30", 4.0, False, 0),
    ("×",   "休み",       "", "", 0.0, False, 0),
    ("年",  "年休（全休）", "", "", 0.0, False, 1.0),
    ("年am", "年休（午前半休）", "", "", 4.0, False, 0.5),
    ("年pm", "年休（午後半休）", "", "", 4.0, False, 0.5),
    # 以下は希望があった場合のみ使う特別区分（必要人数の対象外、希望があれば100%反映）
    ("出",  "出張",       "", "", 0.0, False, 0),
    ("実",  "実習",       "", "", 0.0, False, 0),
    ("研",  "研修",       "", "", 0.0, False, 0),
    ("産",  "産休",       "", "", 0.0, False, 0),
    ("育",  "育休",       "", "", 0.0, False, 0),
]

# 必要人数の設定・自動生成の対象となる「通常勤務」コード（特別区分は含まない）
STANDARD_WORK_CODES = ["N", "準", "入", "am", "pm"]
# 希望があれば使う特別区分（自動生成では必要人数の対象にせず、希望があれば必ず反映する）
SPECIAL_CODES = ["出", "実", "研", "産", "育"]
# 有給・半休系
LEAVE_CODES = ["年", "年am", "年pm"]

WEEKDAY_JP = ["月", "火", "水", "木", "金", "土", "日"]


# ─────────────────────────────────────────────
# DB初期化
# ─────────────────────────────────────────────
def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_conn()
    c = conn.cursor()

    # 職員マスター
    c.execute("""
        CREATE TABLE IF NOT EXISTS staff (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            job_type TEXT NOT NULL,           -- 看護師 / 介護士
            employment_type TEXT NOT NULL,    -- 常勤 / パート / 扶養内
            monthly_hour_limit INTEGER,       -- パート等の月間労働時間上限（常勤はNULL）
            night_shift_ok INTEGER NOT NULL DEFAULT 1,  -- 1=可, 0=不可
            night_shift_target INTEGER DEFAULT 4,       -- 月間目標夜勤回数
            am_pm_eligible INTEGER NOT NULL DEFAULT 1,  -- 午前(am)/午後(pm)勤務に対応できるか（1=可、基本は可でOK。不可の人だけ個別にチェックを外す）
            hire_date TEXT NOT NULL,          -- 入社年月日 YYYY-MM-DD
            active INTEGER NOT NULL DEFAULT 1,
            note TEXT
        )
    """)

    # 簡易マイグレーション: 旧バージョンのDBにam_pm_eligibleが無い場合は追加
    existing_staff_cols = [row[1] for row in c.execute("PRAGMA table_info(staff)").fetchall()]
    if "am_pm_eligible" not in existing_staff_cols:
        c.execute("ALTER TABLE staff ADD COLUMN am_pm_eligible INTEGER NOT NULL DEFAULT 1")
    else:
        # 以前のバージョンでデフォルト0のまま登録されたデータを救済（誰か1人でも
        # 意図的に1にしていたら、その調整は尊重してスキップする）
        total = c.execute("SELECT COUNT(*) FROM staff").fetchone()[0]
        eligible_count = c.execute("SELECT COUNT(*) FROM staff WHERE am_pm_eligible=1").fetchone()[0]
        if total > 0 and eligible_count == 0:
            c.execute("UPDATE staff SET am_pm_eligible=1")
    conn.commit()

    # 職員ごとのシフト制約（希望休み・希望勤務・時間縛り等）月単位で管理
    c.execute("""
        CREATE TABLE IF NOT EXISTS staff_constraints (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            staff_id INTEGER NOT NULL,
            target_month TEXT NOT NULL,   -- YYYY-MM
            constraint_date TEXT NOT NULL, -- YYYY-MM-DD
            constraint_type TEXT NOT NULL, -- 常に"希望"（shift_codeに実際の希望コードを直接格納するシンプル設計）
            shift_code TEXT,               -- 希望勤務の場合のシフトコード
            memo TEXT,
            FOREIGN KEY (staff_id) REFERENCES staff(id) ON DELETE CASCADE
        )
    """)

    # 有給休暇の残日数・消化履歴
    c.execute("""
        CREATE TABLE IF NOT EXISTS paid_leave_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            staff_id INTEGER NOT NULL,
            used_date TEXT NOT NULL,   -- YYYY-MM-DD
            amount REAL NOT NULL DEFAULT 1.0,  -- 消費日数（1.0=全休, 0.5=半休）
            source TEXT NOT NULL DEFAULT '手動',  -- '自動'（シフト生成時に自動記録）/ '手動'（人力で登録・修正）
            note TEXT,
            FOREIGN KEY (staff_id) REFERENCES staff(id) ON DELETE CASCADE
        )
    """)

    # 簡易マイグレーション: 旧バージョンのDBにamount/sourceが無い場合は追加
    existing_leave_cols = [row[1] for row in c.execute("PRAGMA table_info(paid_leave_usage)").fetchall()]
    if "amount" not in existing_leave_cols:
        c.execute("ALTER TABLE paid_leave_usage ADD COLUMN amount REAL NOT NULL DEFAULT 1.0")
    if "source" not in existing_leave_cols:
        c.execute("ALTER TABLE paid_leave_usage ADD COLUMN source TEXT NOT NULL DEFAULT '手動'")
    conn.commit()

    # シフト区分マスター
    c.execute("""
        CREATE TABLE IF NOT EXISTS shift_types (
            code TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            start_time TEXT,
            end_time TEXT,
            hours REAL NOT NULL,
            is_night INTEGER NOT NULL DEFAULT 0,
            leave_amount REAL NOT NULL DEFAULT 0   -- 有給としての消費日数（1.0=全休, 0.5=半休, 0=通常勤務や休み）
        )
    """)

    # 簡易マイグレーション: 旧バージョンのDBにleave_amountが無い場合は追加
    existing_shift_cols = [row[1] for row in c.execute("PRAGMA table_info(shift_types)").fetchall()]
    if "leave_amount" not in existing_shift_cols:
        c.execute("ALTER TABLE shift_types ADD COLUMN leave_amount REAL NOT NULL DEFAULT 0")
    conn.commit()

    # 曜日別・シフト種別ごとの必要人数設定（職種別）
    c.execute("""
        CREATE TABLE IF NOT EXISTS staffing_requirements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            weekday INTEGER NOT NULL,     -- 0=月 ... 6=日
            shift_code TEXT NOT NULL,
            job_type TEXT NOT NULL,       -- 看護師 / 介護士
            required_count INTEGER NOT NULL DEFAULT 0,
            is_bath_day INTEGER NOT NULL DEFAULT 0,  -- 入浴日など特別対応日か
            UNIQUE(weekday, shift_code, job_type)
        )
    """)

    # 日付単位の特別対応日（イレギュラーで特定の日だけ最低人数を変えたい場合）
    c.execute("""
        CREATE TABLE IF NOT EXISTS date_overrides (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            target_date TEXT NOT NULL,   -- YYYY-MM-DD
            shift_code TEXT NOT NULL,
            job_type TEXT NOT NULL,
            required_count INTEGER NOT NULL DEFAULT 0,
            UNIQUE(target_date, shift_code, job_type)
        )
    """)

    # 夜勤の柔軟配置設定（施設全体で1つ）
    c.execute("""
        CREATE TABLE IF NOT EXISTS night_shift_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            standard_nurse INTEGER NOT NULL DEFAULT 1,
            standard_care INTEGER NOT NULL DEFAULT 2,
            allow_flex INTEGER NOT NULL DEFAULT 1,   -- 介護士不足時、看護師で補填してよいか
            flex_nurse INTEGER NOT NULL DEFAULT 2,
            flex_care INTEGER NOT NULL DEFAULT 1,
            max_consecutive_days INTEGER NOT NULL DEFAULT 4,  -- 施設運用ルール（法律の絶対上限ではない）
            use_three_shift INTEGER NOT NULL DEFAULT 0  -- 0=2交代制(日勤/入/明のみ) 1=3交代制(準夜勤も使う)
        )
    """)
    c.execute("INSERT OR IGNORE INTO night_shift_settings (id) VALUES (1)")

    # 簡易マイグレーション: 以前のバージョンのDBに新しいカラムが無い場合、追加する
    existing_cols = [row[1] for row in c.execute("PRAGMA table_info(night_shift_settings)").fetchall()]
    if "use_three_shift" not in existing_cols:
        c.execute("ALTER TABLE night_shift_settings ADD COLUMN use_three_shift INTEGER NOT NULL DEFAULT 0")
    conn.commit()

    # 月またぎ夜勤（前月最終日が夜勤で、当月1日への影響がある場合の入力）
    c.execute("""
        CREATE TABLE IF NOT EXISTS cross_month_night (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            staff_id INTEGER NOT NULL,
            target_month TEXT NOT NULL,  -- YYYY-MM（当月）
            prev_month_last_shift TEXT,  -- 前月末日の勤務コード（例: 入）
            FOREIGN KEY (staff_id) REFERENCES staff(id) ON DELETE CASCADE
        )
    """)

    # 生成済みシフト結果（月単位で保存）
    c.execute("""
        CREATE TABLE IF NOT EXISTS generated_shifts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            staff_id INTEGER NOT NULL,
            target_month TEXT NOT NULL,
            work_date TEXT NOT NULL,
            shift_code TEXT NOT NULL,
            FOREIGN KEY (staff_id) REFERENCES staff(id) ON DELETE CASCADE,
            UNIQUE(staff_id, work_date)
        )
    """)

    conn.commit()

    # シフト区分の初期データ投入
    c.execute("SELECT COUNT(*) FROM shift_types")
    if c.fetchone()[0] == 0:
        for code, name, start, end, hours, is_night, leave_amt in DEFAULT_SHIFT_TYPES:
            c.execute(
                "INSERT INTO shift_types (code, name, start_time, end_time, hours, is_night, leave_amount) VALUES (?,?,?,?,?,?,?)",
                (code, name, start, end, hours, int(is_night), leave_amt),
            )
        conn.commit()

    # 曜日別の最低人数指定：初期値は基本0（=システムにお任せ）。
    # 入浴日（火木土）だけ「最低限必要な理由がある」例として最小限を入れておく。
    c.execute("SELECT COUNT(*) FROM staffing_requirements")
    if c.fetchone()[0] == 0:
        for weekday in range(7):
            for shift_code in ["N", "準", "am", "pm"]:
                for job_type in ["看護師", "介護士"]:
                    is_bath = 1 if weekday in (1, 3, 5) and shift_code == "N" else 0
                    cnt = 1 if (is_bath and job_type == "介護士") else 0  # 入浴日の日勤介護士のみ例として最低1名
                    c.execute(
                        "INSERT OR IGNORE INTO staffing_requirements (weekday, shift_code, job_type, required_count, is_bath_day) VALUES (?,?,?,?,?)",
                        (weekday, shift_code, job_type, cnt, is_bath),
                    )
        conn.commit()

    conn.close()


def seed_sample_staff():
    """初回起動時の動作確認用サンプル職員データ（看護師20名・介護士20名）"""
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM staff")
    if c.fetchone()[0] > 0:
        conn.close()
        return

    nurse_surnames = ["佐藤", "鈴木", "高橋", "田中", "伊藤", "渡辺", "山本", "中村", "小林", "加藤",
                       "吉田", "山田", "佐々木", "山口", "松本", "井上", "木村", "林", "斎藤", "清水"]
    care_surnames = ["森", "池田", "橋本", "阿部", "石川", "山下", "中島", "石井", "小川", "前田",
                      "岡田", "長谷川", "藤田", "後藤", "近藤", "村上", "遠藤", "青木", "坂本", "福田"]

    today = date.today()
    rows = []
    for i, name in enumerate(nurse_surnames):
        # 入社年月日をばらけさせる（新人〜ベテランを混在させる）
        years_ago = 1 + (i % 8)
        hire = today.replace(year=today.year - years_ago)
        emp_type = "常勤" if i % 5 != 0 else "パート"
        limit_h = None if emp_type == "常勤" else 100
        night_ok = 1 if i % 6 != 0 else 0  # 一部は夜勤不可（例: 育児中等を想定）
        rows.append((f"{name} 看護師{i+1:02d}", "看護師", emp_type, limit_h, night_ok, 4, hire.isoformat(), 1, ""))

    for i, name in enumerate(care_surnames):
        years_ago = 1 + (i % 6)
        hire = today.replace(year=today.year - years_ago)
        emp_type = "常勤" if i % 4 != 0 else ("パート" if i % 8 != 0 else "扶養内")
        limit_h = None if emp_type == "常勤" else (100 if emp_type == "パート" else 80)
        night_ok = 1 if i % 5 != 0 else 0
        rows.append((f"{name} 介護士{i+1:02d}", "介護士", emp_type, limit_h, night_ok, 4, hire.isoformat(), 1, ""))

    c.executemany(
        """INSERT INTO staff (name, job_type, employment_type, monthly_hour_limit,
           night_shift_ok, night_shift_target, hire_date, active, note)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        rows,
    )
    conn.commit()
    conn.close()


# ─────────────────────────────────────────────
# 有給休暇 自動計算ロジック（労働基準法第39条 準拠）
# ─────────────────────────────────────────────
# 週所定労働日数が5日以上（または週30時間以上）のフルタイム基準。
# 継続勤務年数 0.5→10日、1.5→11日、2.5→12日、3.5→14日、4.5→16日、5.5→18日、6.5以上→20日
PAID_LEAVE_TABLE = [
    (0.5, 10), (1.5, 11), (2.5, 12), (3.5, 14),
    (4.5, 16), (5.5, 18), (6.5, 20),
]


def calc_years_of_service(hire_date_str: str, as_of: date = None) -> float:
    """入社日からの勤続年数（小数）を返す"""
    if as_of is None:
        as_of = date.today()
    hire = datetime.strptime(hire_date_str, "%Y-%m-%d").date()
    days = (as_of - hire).days
    if days < 0:
        return 0.0
    return days / 365.25


def calc_paid_leave_grant_days(hire_date_str: str, as_of: date = None) -> int:
    """
    法定付与日数を返す。労基法第39条の基準日ルールに基づき、
    「6ヶ月経過で10日、以後1年ごとに直近の基準日に達した段階の日数」を返す簡易実装。
    ※ 出勤率8割要件はこの簡易版では考慮していません。実運用では出勤率も併せて確認してください。
    """
    years = calc_years_of_service(hire_date_str, as_of)
    if years < 0.5:
        return 0
    granted = 0
    for threshold, days in PAID_LEAVE_TABLE:
        if years >= threshold:
            granted = days
    return granted


def get_auto_cross_month_night_staff(target_month: str) -> set:
    """
    前月がこのシステムで既に自動生成されていれば、前月末日に「入」だった職員を
    自動的に検出して返す（手動入力不要にするための機能）。
    前月データが無い場合は空集合を返す（その場合は手動入力にフォールバック）。
    """
    year, month = map(int, target_month.split("-"))
    if month == 1:
        prev_year, prev_month = year - 1, 12
    else:
        prev_year, prev_month = year, month - 1
    prev_last_day = date(prev_year, prev_month, calendar.monthrange(prev_year, prev_month)[1])

    conn = get_conn()
    rows = conn.execute(
        "SELECT staff_id FROM generated_shifts WHERE work_date = ? AND shift_code = '入'",
        (prev_last_day.isoformat(),),
    ).fetchall()
    conn.close()
    return {r[0] for r in rows}


def calc_paid_leave_balance(staff_id: int, hire_date_str: str) -> dict:
    """付与日数・消化日数（半休は0.5日として計算）・残日数をまとめて返す"""
    granted = calc_paid_leave_grant_days(hire_date_str)
    conn = get_conn()
    row = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM paid_leave_usage WHERE staff_id = ?", (staff_id,)
    ).fetchone()
    conn.close()
    used = row[0] if row and row[0] is not None else 0.0
    return {"granted": granted, "used": used, "remaining": max(0.0, granted - used)}


# ─────────────────────────────────────────────
# Streamlit 画面
# ─────────────────────────────────────────────
st.set_page_config(page_title="勤務表自動生成システム", page_icon="🗓️", layout="wide")

# 少しだけ見た目を整えるカスタムCSS（タブを大きめ・見やすく、余白の調整）
st.markdown("""
<style>
    .stTabs [data-baseweb="tab-list"] { gap: 4px; }
    .stTabs [data-baseweb="tab"] {
        height: 46px; white-space: pre-wrap; border-radius: 8px 8px 0 0;
        font-weight: 600; font-size: 14px;
    }
    div[data-testid="stExpander"] { border-radius: 10px; border: 1px solid #E0E6E3; }
    div[data-testid="stMetric"] { background: #F7FAF9; border-radius: 10px; padding: 10px; }
    h1, h2, h3 { color: #1F3B33; }
</style>
""", unsafe_allow_html=True)

init_db()
seed_sample_staff()

st.title("🗓️ 勤務表自動生成システム")
st.caption("看護師・介護士向け シフト管理ツール（試作版）")

tab_names = ["👥 職員登録・一覧", "📅 希望シフト一括入力", "🌴 有給管理", "⚙️ シフト・条件設定", "🤖 シフト自動生成", "💾 バックアップ"]
tabs = st.tabs(tab_names)

# ═════════════════════════════════════════════
# TAB 1: 職員登録・一覧
# ═════════════════════════════════════════════
with tabs[0]:
    st.subheader("👥 職員の登録・編集")
    st.caption("ここで登録した職員が、有給管理・シフト自動生成のすべての基礎データになります。")

    conn = get_conn()
    staff_df = pd.read_sql_query("SELECT * FROM staff ORDER BY job_type, name", conn)
    conn.close()

    with st.expander("➕ 新しい職員を登録する", expanded=(len(staff_df) == 0)):
        with st.form("add_staff_form", clear_on_submit=True):
            col1, col2 = st.columns(2)
            with col1:
                f_name = st.text_input("職員名 *")
                f_job = st.selectbox("職種 *", ["看護師", "介護士"])
                f_emp = st.selectbox("雇用形態 *", ["常勤", "パート", "扶養内"])
                f_hire = st.date_input("入社年月日 *", value=date.today(),
                                       min_value=date(1950, 1, 1), max_value=date.today())
            with col2:
                f_limit = None
                if f_emp in ("パート", "扶養内"):
                    f_limit = st.number_input("月間労働時間上限（時間）", min_value=1, max_value=200, value=80)
                f_night_ok = st.checkbox("夜勤可能", value=True)
                f_night_target = st.number_input("月間目標夜勤回数", min_value=0, max_value=15, value=4)
                f_am_pm = st.checkbox("午前(am)・午後(pm)勤務に対応できる", value=True,
                                       help="チェックを入れた職員だけが、自動生成でam/pm勤務の必要人数の対象になります。")
                f_note = st.text_input("備考（任意）")

            submitted = st.form_submit_button("登録する", type="primary")
            if submitted:
                if not f_name.strip():
                    st.error("職員名を入力してください。")
                else:
                    conn = get_conn()
                    conn.execute(
                        """INSERT INTO staff (name, job_type, employment_type, monthly_hour_limit,
                           night_shift_ok, night_shift_target, am_pm_eligible, hire_date, active, note)
                           VALUES (?,?,?,?,?,?,?,?,?,?)""",
                        (f_name.strip(), f_job, f_emp, f_limit, int(f_night_ok),
                         f_night_target, int(f_am_pm), f_hire.isoformat(), 1, f_note),
                    )
                    conn.commit()
                    conn.close()
                    st.success(f"{f_name} さんを登録しました。")
                    st.rerun()

    st.divider()
    st.subheader("📋 職員一覧")

    with st.expander("🔧 全員まとめて「午前(am)/午後(pm)対応可」にする（うまく反映されない場合はこちら）"):
        st.caption("個別に不可の人がいる場合は、下の一覧からその人だけチェックを外してください。")
        if st.button("全員を「対応可」に一括設定する"):
            conn = get_conn()
            conn.execute("UPDATE staff SET am_pm_eligible=1")
            conn.commit()
            conn.close()
            st.success("全員を「対応可」にしました。")
            st.rerun()

    col_a, col_b, col_c = st.columns(3)
    with col_a:
        filter_job = st.multiselect("職種で絞り込み", ["看護師", "介護士"], default=["看護師", "介護士"])
    with col_b:
        filter_active = st.selectbox("状態", ["在籍中のみ", "全員（退職者含む）"])
    with col_c:
        st.write("")

    view_df = staff_df[staff_df["job_type"].isin(filter_job)] if len(staff_df) else staff_df
    if filter_active == "在籍中のみ" and len(view_df):
        view_df = view_df[view_df["active"] == 1]

    if len(view_df) == 0:
        st.info("該当する職員がいません。")
    else:
        # 「看護師 常勤」「看護師 パート」「介護士 常勤」「介護士 パート」のようにグループ化して表示
        group_order = [
            ("看護師", "常勤"), ("看護師", "パート"), ("看護師", "扶養内"),
            ("介護士", "常勤"), ("介護士", "パート"), ("介護士", "扶養内"),
        ]
        for job_type, emp_type in group_order:
            group_df = view_df[(view_df["job_type"] == job_type) & (view_df["employment_type"] == emp_type)]
            if len(group_df) == 0:
                continue
            st.markdown(f"#### {'🩺' if job_type == '看護師' else '🧑‍⚕️'} {job_type}　{emp_type}　（{len(group_df)}名）")
            for _, row in group_df.iterrows():
                years = calc_years_of_service(row["hire_date"])
                with st.expander(
                    f"{'🩺' if row['job_type']=='看護師' else '🧑‍⚕️'} {row['name']}"
                    f"　({row['job_type']} / {row['employment_type']} / 勤続{years:.1f}年)"
                    f"{'' if row['active'] else '　🔴退職済'}"
                ):
                    ec1, ec2 = st.columns(2)
                    with ec1:
                        e_name = st.text_input("職員名", value=row["name"], key=f"name_{row['id']}")
                        e_job = st.selectbox("職種", ["看護師", "介護士"],
                                              index=["看護師", "介護士"].index(row["job_type"]), key=f"job_{row['id']}")
                        e_emp = st.selectbox("雇用形態", ["常勤", "パート", "扶養内"],
                                              index=["常勤", "パート", "扶養内"].index(row["employment_type"]), key=f"emp_{row['id']}")
                        e_hire = st.date_input("入社年月日",
                                                value=datetime.strptime(row["hire_date"], "%Y-%m-%d").date(),
                                                min_value=date(1950, 1, 1), max_value=date.today(),
                                                key=f"hire_{row['id']}")
                    with ec2:
                        e_limit = row["monthly_hour_limit"] if pd.notna(row["monthly_hour_limit"]) and row["monthly_hour_limit"] else 80
                        if e_emp in ("パート", "扶養内"):
                            e_limit = st.number_input("月間労働時間上限（時間）", min_value=1, max_value=200,
                                                        value=int(e_limit), key=f"limit_{row['id']}")
                        else:
                            e_limit = None
                            st.caption("常勤のため時間上限なし")
                        e_night_ok = st.checkbox("夜勤可能", value=bool(row["night_shift_ok"]), key=f"nok_{row['id']}")
                        e_night_target = st.number_input("月間目標夜勤回数", min_value=0, max_value=15,
                                                           value=int(row["night_shift_target"] or 0), key=f"ntgt_{row['id']}")
                        e_am_pm = st.checkbox("午前(am)・午後(pm)勤務に対応できる",
                                               value=bool(row["am_pm_eligible"]) if "am_pm_eligible" in row.index else False,
                                               key=f"ampm_{row['id']}")
                        e_active = st.checkbox("在籍中", value=bool(row["active"]), key=f"active_{row['id']}")
                    e_note = st.text_input("備考", value=row["note"] or "", key=f"note_{row['id']}")

                    bcol1, bcol2 = st.columns([1, 1])
                    with bcol1:
                        if st.button("💾 更新を保存", key=f"save_{row['id']}"):
                            conn = get_conn()
                            conn.execute(
                                """UPDATE staff SET name=?, job_type=?, employment_type=?, monthly_hour_limit=?,
                                   night_shift_ok=?, night_shift_target=?, am_pm_eligible=?, hire_date=?, active=?, note=?
                                   WHERE id=?""",
                                (e_name, e_job, e_emp, e_limit, int(e_night_ok), e_night_target, int(e_am_pm),
                                 e_hire.isoformat(), int(e_active), e_note, row["id"]),
                            )
                            conn.commit()
                            conn.close()
                            st.success("更新しました。")
                            st.rerun()
                    with bcol2:
                        if st.button("🗑️ この職員を削除", key=f"del_{row['id']}"):
                            conn = get_conn()
                            conn.execute("DELETE FROM staff WHERE id=?", (row["id"],))
                            conn.commit()
                            conn.close()
                            st.warning(f"{row['name']} さんを削除しました。")
                            st.rerun()

# ═════════════════════════════════════════════
# TAB (新設): 希望シフト一括入力（カレンダー形式）
# ═════════════════════════════════════════════
with tabs[1]:
    st.subheader("📅 希望シフト一括入力（カレンダー形式）")
    st.caption(
        "職員名を縦に、日付を横に並べた表形式で、まとめて希望を入力できます。"
        "セルに直接コードを入力してください（例: 休＝希望休み、有＝有給希望、"
        "N・準・入・E・AM・PM＝希望勤務）。空欄は「希望なし（自動生成にお任せ）」という意味になります。"
    )

    conn = get_conn()
    staff_df = pd.read_sql_query(
        "SELECT id, name, job_type FROM staff WHERE active=1 ORDER BY job_type, name", conn
    )
    conn.close()

    if len(staff_df) == 0:
        st.info("在籍中の職員が登録されていません。先に「職員登録・一覧」タブで登録してください。")
    else:
        grid_month = st.text_input("対象月 (YYYY-MM)", value=date.today().strftime("%Y-%m"), key="grid_month")
        try:
            g_year, g_month = map(int, grid_month.split("-"))
            g_days_in_month = calendar.monthrange(g_year, g_month)[1]
        except Exception:
            st.error("月の形式が正しくありません（例: 2026-08）。")
            g_days_in_month = 0

        if g_days_in_month:
            date_cols = []
            for d in range(1, g_days_in_month + 1):
                wd = date(g_year, g_month, d).weekday()
                date_cols.append(f"{d}({WEEKDAY_JP[wd]})")

            # 職員名で絞り込み（対象の職員をすぐ見つけられるように）
            search_name = st.text_input("🔍 職員名で絞り込み（下にスクロールしなくても見つけやすくなります）", value="")
            filtered_staff_df = staff_df[staff_df["name"].str.contains(search_name, na=False)] if search_name else staff_df

            if len(filtered_staff_df) == 0:
                st.warning("該当する職員が見つかりませんでした。")
            else:
                # 既存の登録済み希望を読み込んで初期表示に反映する
                conn = get_conn()
                existing = pd.read_sql_query(
                    "SELECT * FROM staff_constraints WHERE target_month=?", conn, params=(grid_month,)
                )
                conn.close()

                grid_data = {"職員名": filtered_staff_df["name"].tolist()}
                for col in date_cols:
                    grid_data[col] = ["" for _ in range(len(filtered_staff_df))]

                # 職員名をインデックスにすると、横にスクロールしても左端に固定表示される
                grid_df = pd.DataFrame(grid_data).set_index("職員名")

                # 既存データをグリッドに反映
                id_to_name = dict(zip(filtered_staff_df["id"], filtered_staff_df["name"]))
                for _, erow in existing.iterrows():
                    sid = erow["staff_id"]
                    if sid not in id_to_name:
                        continue
                    try:
                        d_num = int(erow["constraint_date"].split("-")[2])
                    except Exception:
                        continue
                    col_match = [c for c in date_cols if c.startswith(f"{d_num}(")]
                    if not col_match:
                        continue
                    col = col_match[0]
                    grid_df.at[id_to_name[sid], col] = erow["shift_code"] or ""

                st.markdown(
                    "**日付のセルをタップすると、希望コードを選べます。**　"
                    "×=休み希望／年・年am・年pm=有給希望（全休/午前半休/午後半休）／"
                    "N・準・入・明・am・pm=勤務希望／出・実・研・産・育=特別区分の希望／空欄=希望なし"
                )
                lock_edit = st.checkbox(
                    "🔒 入力が終わったので編集をロックする（誤って他の人の欄を押してしまう事故を防げます）",
                    value=False, key=f"lock_{grid_month}",
                )
                requestable_codes = [""] + [s[0] for s in DEFAULT_SHIFT_TYPES]
                column_config = {}
                for col in date_cols:
                    column_config[col] = st.column_config.SelectboxColumn(
                        col, options=requestable_codes, required=False, width="small",
                    )
                edited_grid = st.data_editor(
                    grid_df,
                    use_container_width=True,
                    height=min(70 + 35 * len(filtered_staff_df), 700),
                    disabled=lock_edit,
                    column_config=column_config,
                    key=f"shift_request_grid_{grid_month}",
                )
                if lock_edit:
                    st.info("🔒 現在ロック中です。編集するには、上のチェックを外してください。")

                if st.button("💾 このカレンダーの内容を保存する", type="primary", disabled=lock_edit):
                    valid_codes = {s[0] for s in DEFAULT_SHIFT_TYPES}
                    name_to_id = dict(zip(filtered_staff_df["name"], filtered_staff_df["id"]))

                    conn = get_conn()
                    # 絞り込み表示中の職員分だけを対象に、当月分の既存の希望を一旦削除してから作り直す
                    staff_id_list = filtered_staff_df["id"].tolist()
                    conn.execute(
                        f"DELETE FROM staff_constraints WHERE target_month=? AND staff_id IN ({','.join('?' * len(staff_id_list))})",
                        [grid_month] + staff_id_list,
                    )

                    inserted = 0
                    for staff_name, grow in edited_grid.iterrows():
                        sid = name_to_id.get(staff_name)
                        if sid is None:
                            continue
                        for col in date_cols:
                            val = str(grow[col]).strip()
                            if not val or val == "nan":
                                continue
                            if val not in valid_codes:
                                continue  # 誤入力防止のため、無効なコードは保存されません
                            d_num = int(col.split("(")[0])
                            c_date = date(g_year, g_month, d_num).isoformat()
                            conn.execute(
                                """INSERT INTO staff_constraints (staff_id, target_month, constraint_date,
                                   constraint_type, shift_code, memo) VALUES (?,?,?,?,?,?)""",
                                (sid, grid_month, c_date, "希望", val, ""),
                            )
                            inserted += 1
                    conn.commit()
                    conn.close()
                    st.success(f"{inserted}件の希望を保存しました。")
                    st.rerun()

    st.divider()
    st.markdown("### 📆 期間で一括登録（産休・育休・出張・実習・研修など）")
    st.caption(
        "「◯月◯日から◯月◯日まで、ずっと育休」のような、まとまった期間の登録に便利です。"
        "上のカレンダーに1日ずつ入力する必要はありません。"
    )
    conn = get_conn()
    all_staff_df = pd.read_sql_query("SELECT id, name FROM staff WHERE active=1 ORDER BY name", conn)
    conn.close()
    if len(all_staff_df):
        bc1, bc2 = st.columns(2)
        with bc1:
            b_staff = st.selectbox("職員", all_staff_df["name"].tolist(), key="bulk_staff")
        with bc2:
            b_code = st.selectbox(
                "登録する内容", ["産", "育", "出", "実", "研", "年", "×"],
                format_func=lambda x: {"産": "産休", "育": "育休", "出": "出張", "実": "実習",
                                        "研": "研修", "年": "年休（連続取得）", "×": "休み（連続）"}[x],
                key="bulk_code",
            )
        bc3, bc4 = st.columns(2)
        with bc3:
            b_start = st.date_input("開始日", value=date.today(), key="bulk_start")
        with bc4:
            b_end = st.date_input("終了日", value=date.today(), key="bulk_end")

        if b_end < b_start:
            st.error("終了日は開始日以降にしてください。")
        else:
            days_count = (b_end - b_start).days + 1
            st.caption(f"対象期間: {days_count}日間")
            if st.button("この期間で一括登録する", type="primary"):
                staff_id = int(all_staff_df[all_staff_df["name"] == b_staff]["id"].iloc[0])
                conn = get_conn()
                inserted = 0
                cur_date = b_start
                while cur_date <= b_end:
                    ym = cur_date.strftime("%Y-%m")
                    # 同じ日・同じ職員の既存の希望があれば削除してから登録し直す（上書き）
                    conn.execute(
                        "DELETE FROM staff_constraints WHERE staff_id=? AND constraint_date=?",
                        (staff_id, cur_date.isoformat()),
                    )
                    conn.execute(
                        """INSERT INTO staff_constraints (staff_id, target_month, constraint_date,
                           constraint_type, shift_code, memo) VALUES (?,?,?,?,?,?)""",
                        (staff_id, ym, cur_date.isoformat(), "希望", b_code, ""),
                    )
                    inserted += 1
                    cur_date += timedelta(days=1)
                conn.commit()
                conn.close()
                st.success(f"{b_staff} さんに、{b_start}〜{b_end}（{inserted}日間）の登録をしました。")
                st.rerun()

# ═════════════════════════════════════════════
# TAB 2: 有給管理
# ═════════════════════════════════════════════
with tabs[2]:
    st.subheader("🌴 有給休暇の管理")
    st.caption(
        "入社年月日をもとに、労働基準法第39条の基準（週5日以上勤務のフルタイム基準）で"
        "法定付与日数を自動計算します。パート等、所定労働日数が少ない方は法定日数が異なるため、"
        "実務では就業規則・比例付与表もあわせてご確認ください。"
    )

    conn = get_conn()
    staff_df = pd.read_sql_query("SELECT * FROM staff WHERE active=1 ORDER BY job_type, name", conn)
    conn.close()

    if len(staff_df) == 0:
        st.info("在籍中の職員が登録されていません。")
    else:
        rows = []
        for _, row in staff_df.iterrows():
            bal = calc_paid_leave_balance(row["id"], row["hire_date"])
            years = calc_years_of_service(row["hire_date"])
            rows.append({
                "職員名": row["name"],
                "職種": row["job_type"],
                "入社年月日": row["hire_date"],
                "勤続年数": f"{years:.1f}年",
                "法定付与日数": bal["granted"],
                "消化日数": bal["used"],
                "残日数": bal["remaining"],
            })
        leave_df = pd.DataFrame(rows)
        st.dataframe(leave_df, use_container_width=True, hide_index=True)
        st.caption(
            "「消化日数」は、シフト自動生成で「年」（全休）「年am」「年pm」（半休）が割り当てられると"
            "自動的に加算されます。半休は0.5日として計算しています。"
        )

        st.divider()
        st.markdown("**✅ 有給消化の記録を手動で追加する**")
        st.caption("自動生成を使わず紙の勤務表等で先に有給を取得した場合など、手動での記録用です。")
        lc1, lc2, lc3, lc4 = st.columns(4)
        with lc1:
            l_staff = st.selectbox("職員を選択", staff_df["name"].tolist(), key="leave_staff_select")
        with lc2:
            l_date = st.date_input("取得日", value=date.today(), key="leave_date_input")
        with lc3:
            l_amount = st.selectbox("消化日数", [1.0, 0.5], format_func=lambda x: "全休(1.0日)" if x == 1.0 else "半休(0.5日)",
                                     key="leave_amount_input")
        with lc4:
            l_note = st.text_input("備考（任意）", key="leave_note_input")

        if st.button("この記録を追加", type="primary"):
            staff_id = int(staff_df[staff_df["name"] == l_staff]["id"].iloc[0])
            conn = get_conn()
            conn.execute(
                "INSERT INTO paid_leave_usage (staff_id, used_date, amount, source, note) VALUES (?,?,?,?,?)",
                (staff_id, l_date.isoformat(), l_amount, "手動", l_note),
            )
            conn.commit()
            conn.close()
            st.success(f"{l_staff} さんの有給消化（{l_date}、{l_amount}日）を記録しました。")
            st.rerun()

        st.divider()
        st.markdown("**🔧 消化記録の確認・修正・削除**")
        st.caption(
            "自動記録・手動記録どちらも、間違いに気づいたらここから直接修正・削除できます"
            "（数え間違いや、打刻ミスの訂正などにお使いください）。"
        )
        conn = get_conn()
        usage_df = pd.read_sql_query(
            """SELECT plu.id, s.name as 職員名, plu.used_date as 取得日, plu.amount as 消化日数,
               plu.source as 記録方法, plu.note as 備考
               FROM paid_leave_usage plu JOIN staff s ON plu.staff_id = s.id
               ORDER BY plu.used_date DESC""",
            conn,
        )
        conn.close()
        if len(usage_df):
            st.dataframe(usage_df, use_container_width=True, hide_index=True)

            fix_id = st.selectbox("修正・削除する記録のID", [0] + usage_df["id"].tolist(), key="fix_usage_id")
            if fix_id:
                target_row = usage_df[usage_df["id"] == fix_id].iloc[0]
                fc1, fc2, fc3 = st.columns(3)
                with fc1:
                    fix_date = st.date_input("取得日を修正", value=datetime.strptime(target_row["取得日"], "%Y-%m-%d").date(),
                                              key="fix_date")
                with fc2:
                    fix_amount = st.selectbox("消化日数を修正", [1.0, 0.5],
                                               index=0 if target_row["消化日数"] == 1.0 else 1,
                                               format_func=lambda x: "全休(1.0日)" if x == 1.0 else "半休(0.5日)",
                                               key="fix_amount")
                with fc3:
                    fix_note = st.text_input("備考を修正", value=target_row["備考"] or "", key="fix_note")

                bcol1, bcol2 = st.columns(2)
                with bcol1:
                    if st.button("💾 この記録を修正して保存"):
                        conn = get_conn()
                        conn.execute(
                            "UPDATE paid_leave_usage SET used_date=?, amount=?, source=?, note=? WHERE id=?",
                            (fix_date.isoformat(), fix_amount, "手動（修正済み）", fix_note, fix_id),
                        )
                        conn.commit()
                        conn.close()
                        st.success("修正しました。")
                        st.rerun()
                with bcol2:
                    if st.button("🗑️ この記録を削除"):
                        conn = get_conn()
                        conn.execute("DELETE FROM paid_leave_usage WHERE id=?", (fix_id,))
                        conn.commit()
                        conn.close()
                        st.warning("削除しました。")
                        st.rerun()
        else:
            st.caption("消化記録はまだありません。")

# ═════════════════════════════════════════════
# TAB 3: シフト・条件設定
# ═════════════════════════════════════════════
with tabs[3]:
    st.subheader("⚙️ シフト区分・必要人数・夜勤ルールの設定")

    sub_tab1, sub_tab2, sub_tab3, sub_tab4 = st.tabs(
        ["🕐 シフト区分", "📅 曜日別の最低人数指定", "🌙 夜勤の配置ルール", "🔀 月またぎ夜勤"]
    )

    # ── シフト区分 ──
    with sub_tab1:
        st.caption("勤務区分（コード・時間帯）を編集できます。")
        conn = get_conn()
        shift_df = pd.read_sql_query("SELECT * FROM shift_types", conn)
        conn.close()
        st.dataframe(shift_df, use_container_width=True, hide_index=True)

        with st.form("edit_shift_type_form"):
            st.markdown("**シフト区分を編集**")
            ec1, ec2, ec3 = st.columns(3)
            with ec1:
                target_code = st.selectbox("編集する区分", shift_df["code"].tolist())
            row = shift_df[shift_df["code"] == target_code].iloc[0]
            with ec2:
                new_start = st.text_input("開始時刻 (HH:MM)", value=row["start_time"] or "")
                new_end = st.text_input("終了時刻 (HH:MM)", value=row["end_time"] or "")
            with ec3:
                new_hours = st.number_input("労働時間（時間）", min_value=0.0, max_value=24.0,
                                             value=float(row["hours"]), step=0.5)
                new_is_night = st.checkbox("夜勤区分として扱う", value=bool(row["is_night"]))
            if st.form_submit_button("この区分を更新"):
                conn = get_conn()
                conn.execute(
                    "UPDATE shift_types SET start_time=?, end_time=?, hours=?, is_night=? WHERE code=?",
                    (new_start, new_end, new_hours, int(new_is_night), target_code),
                )
                conn.commit()
                conn.close()
                st.success("更新しました。")
                st.rerun()

    # ── 曜日別の最低人数指定 ──
    with sub_tab2:
        st.info(
            "💡 **「0」＝最低人数の指定なし（システムにお任せ）**\n\n"
            "例：月曜は入浴介助があるので、最低でも介護士2名は欲しい → その日だけ「2」を指定。"
            "特に理由がない曜日・シフトは「0」のままでOKです。指定しなかった分は、他の制約"
            "（週1休み・希望など）を守りつつ、システムが自動でバランス良く配置します。"
        )
        st.caption("「特別対応日」にチェックを入れると、入浴日など特別な日として記録に残せます（任意）。")
        conn = get_conn()
        req_df = pd.read_sql_query(
            "SELECT * FROM staffing_requirements ORDER BY weekday, shift_code, job_type", conn
        )
        conn.close()

        day_choice = st.selectbox("編集する曜日", list(range(7)), format_func=lambda x: WEEKDAY_JP[x] + "曜日")
        day_df = req_df[req_df["weekday"] == day_choice]
        st.write(f"**{WEEKDAY_JP[day_choice]}曜日の必要人数**")

        edited_rows = []
        for _, r in day_df.iterrows():
            cols = st.columns([2, 2, 2, 2])
            with cols[0]:
                st.write(r["shift_code"])
            with cols[1]:
                st.write(r["job_type"])
            with cols[2]:
                new_cnt = st.number_input("必要人数", min_value=0, max_value=20,
                                           value=int(r["required_count"]), key=f"req_{r['id']}", label_visibility="collapsed")
            with cols[3]:
                new_bath = st.checkbox("特別対応日", value=bool(r["is_bath_day"]), key=f"bath_{r['id']}")
            edited_rows.append((r["id"], new_cnt, new_bath))

        if st.button("この曜日の設定を保存"):
            conn = get_conn()
            for rid, cnt, bath in edited_rows:
                conn.execute(
                    "UPDATE staffing_requirements SET required_count=?, is_bath_day=? WHERE id=?",
                    (cnt, int(bath), rid),
                )
            conn.commit()
            conn.close()
            st.success("保存しました。")
            st.rerun()

        st.divider()
        st.markdown("### 🔁 特定の日だけ変更する（特別対応日）")
        st.caption(
            "入浴介助日がイレギュラーで変わった時など、曜日のルールとは別に「この日だけ」"
            "最低人数を上書きできます。ここで登録した日は、曜日の設定より優先されます。"
        )
        oc1, oc2, oc3, oc4 = st.columns(4)
        with oc1:
            o_date = st.date_input("対象日", value=date.today(), key="override_date")
        with oc2:
            o_code = st.selectbox("シフト区分", ["N", "準", "am", "pm"], key="override_code")
        with oc3:
            o_job = st.selectbox("職種", ["看護師", "介護士"], key="override_job")
        with oc4:
            o_cnt = st.number_input("最低人数", min_value=0, max_value=20, value=1, key="override_cnt")
        if st.button("この日の特別対応を登録"):
            conn = get_conn()
            conn.execute(
                """INSERT INTO date_overrides (target_date, shift_code, job_type, required_count)
                   VALUES (?,?,?,?)
                   ON CONFLICT(target_date, shift_code, job_type) DO UPDATE SET required_count=excluded.required_count""",
                (o_date.isoformat(), o_code, o_job, o_cnt),
            )
            conn.commit()
            conn.close()
            st.success(f"{o_date} の特別対応を登録しました。")
            st.rerun()

        conn = get_conn()
        override_df = pd.read_sql_query(
            "SELECT id, target_date as 日付, shift_code as 区分, job_type as 職種, required_count as 最低人数 "
            "FROM date_overrides ORDER BY target_date", conn,
        )
        conn.close()
        if len(override_df):
            st.dataframe(override_df, use_container_width=True, hide_index=True)
            del_ov_id = st.selectbox("削除する特別対応のID", [0] + override_df["id"].tolist(), key="del_override")
            if del_ov_id and st.button("この特別対応を削除"):
                conn = get_conn()
                conn.execute("DELETE FROM date_overrides WHERE id=?", (del_ov_id,))
                conn.commit()
                conn.close()
                st.rerun()

    # ── 夜勤の配置ルール ──
    with sub_tab3:
        conn = get_conn()
        ns = conn.execute("SELECT * FROM night_shift_settings WHERE id=1").fetchone()
        conn.close()
        # (id, standard_nurse, standard_care, allow_flex, flex_nurse, flex_care, max_consecutive_days, use_three_shift)

        st.markdown("**勤務体制（2交代制 / 3交代制）**")
        st.caption(
            "「準夜勤」を使う病院（3交代制：日勤・準夜勤・深夜勤）と、使わない病院"
            "（2交代制：日勤・夜勤入り〜明けのみ）、どちらにも対応できます。"
        )
        use_3shift = st.checkbox(
            "3交代制を使う（準夜勤あり）", value=bool(ns[7]),
            help="チェックを外すと2交代制になり、シフト表から「準夜勤」が使われなくなります。"
        )

        st.divider()
        st.markdown("**標準の夜勤配置**")
        nc1, nc2 = st.columns(2)
        with nc1:
            std_nurse = st.number_input("看護師 標準人数", min_value=0, max_value=10, value=int(ns[1]))
        with nc2:
            std_care = st.number_input("介護士 標準人数", min_value=0, max_value=10, value=int(ns[2]))

        st.markdown("**介護士が不足する場合の柔軟対応**")
        st.caption("説明の通り、介護士が不足する日は看護師が代わりに夜勤に入る運用を許可できます。")
        allow_flex = st.checkbox("介護士不足時、看護師による代替配置を許可する", value=bool(ns[3]))
        fc1, fc2 = st.columns(2)
        with fc1:
            flex_nurse = st.number_input("代替時の看護師人数", min_value=0, max_value=10, value=int(ns[4]))
        with fc2:
            flex_care = st.number_input("代替時の介護士人数", min_value=0, max_value=10, value=int(ns[5]))

        st.divider()
        st.markdown("**⚠️ 連続勤務日数の上限（施設の運用ルール）**")
        st.caption(
            "労働基準法が定める休日の絶対ルールは「週1日、または4週で4日」の休日確保のみで、"
            "「○連勤まで」という直接の条文はありません。ここで設定する日数は、職員の負担軽減のための"
            "施設独自の運用ルールとしてお使いください（自動生成エンジンはこの日数を守ったうえで、"
            "さらに週1日以上の休日も別途、法律上の絶対ルールとして必ず確保します）。"
        )
        max_consec = st.number_input("施設ルールとしての連続勤務上限日数", min_value=1, max_value=13,
                                       value=int(ns[6]))

        if st.button("夜勤ルールを保存", type="primary"):
            conn = get_conn()
            conn.execute(
                """UPDATE night_shift_settings SET standard_nurse=?, standard_care=?, allow_flex=?,
                   flex_nurse=?, flex_care=?, max_consecutive_days=?, use_three_shift=? WHERE id=1""",
                (std_nurse, std_care, int(allow_flex), flex_nurse, flex_care, max_consec, int(use_3shift)),
            )
            conn.commit()
            conn.close()
            st.success("保存しました。")

    # ── 月またぎ夜勤 ──
    with sub_tab4:
        st.success(
            "✅ **前月分をこのシステムで自動生成していれば、この入力は基本的に不要です。**\n\n"
            "前月末日に「入（夜勤入り）」だった職員は、シフト自動生成のたびに自動で検出され、"
            "当月1日目の生成に反映されます。"
        )
        st.caption(
            "以下の入力は、前月を紙の勤務表など別の方法で管理していた場合や、"
            "自動検出の結果を上書きしたい場合のみお使いください。"
        )
        conn = get_conn()
        staff_df = pd.read_sql_query("SELECT id, name FROM staff WHERE active=1 AND night_shift_ok=1 ORDER BY name", conn)
        conn.close()

        cm1, cm2, cm3 = st.columns(3)
        with cm1:
            cm_staff = st.selectbox("職員", staff_df["name"].tolist() if len(staff_df) else [], key="cm_staff")
        with cm2:
            cm_month = st.text_input("当月 (YYYY-MM)", value=date.today().strftime("%Y-%m"), key="cm_month")
        with cm3:
            cm_shift = st.selectbox("前月末日の勤務コード", ["入"], key="cm_shift",
                                     help="前月末日が「入（夜勤入り）」だった場合のみ登録してください。当月1日目が自動的に「明」で確定します。")

        if len(staff_df) and st.button("この情報を登録"):
            staff_id = int(staff_df[staff_df["name"] == cm_staff]["id"].iloc[0])
            conn = get_conn()
            conn.execute(
                "INSERT INTO cross_month_night (staff_id, target_month, prev_month_last_shift) VALUES (?,?,?)",
                (staff_id, cm_month, cm_shift),
            )
            conn.commit()
            conn.close()
            st.success("登録しました。")
            st.rerun()

        conn = get_conn()
        cross_df = pd.read_sql_query(
            """SELECT cmn.id, s.name as 職員名, cmn.target_month as 当月, cmn.prev_month_last_shift as 前月末日の勤務
               FROM cross_month_night cmn JOIN staff s ON cmn.staff_id = s.id ORDER BY cmn.target_month DESC""",
            conn,
        )
        conn.close()
        if len(cross_df):
            st.dataframe(cross_df, use_container_width=True, hide_index=True)

# ═════════════════════════════════════════════
# シフト自動生成エンジン（OR-Tools CP-SAT）
# ═════════════════════════════════════════════
def build_and_solve_schedule(target_month: str, time_limit_sec: int = 30):
    """
    target_month: "YYYY-MM"
    戻り値: (success: bool, message: str, result_df: DataFrame or None, consult_list: list)

    設計方針:
    - 「絶対に守るルール」はハード制約: 週1日以上の休日（労基法第35条）、
      パート等の月間労働時間上限、夜勤不可の職員を夜勤に入れない、施設の連続勤務上限。
    - 「できるだけ叶えたいルール」はソフト制約（ペナルティの重み付け）: 個人の希望
      （希望休み・有給希望・希望勤務）、夜勤入りの翌日は明けにする、夜勤回数の平準化、
      土日勤務の偏り軽減。マンパワー不足で希望通りにならない場合は、ハード制約を破らない
      範囲で最も希望に近い代替案を提示し、「ご相談」として一覧に残します。
    """
    if not ORTOOLS_AVAILABLE:
        return False, "OR-Tools がインストールされていません。requirements.txt を確認してください。", None, []

    year, month = map(int, target_month.split("-"))
    days_in_month = calendar.monthrange(year, month)[1]
    date_list = [date(year, month, d) for d in range(1, days_in_month + 1)]

    conn = get_conn()
    staff_df = pd.read_sql_query("SELECT * FROM staff WHERE active=1", conn)
    ns = conn.execute("SELECT * FROM night_shift_settings WHERE id=1").fetchone()
    req_df = pd.read_sql_query("SELECT * FROM staffing_requirements", conn)
    cons_df = pd.read_sql_query(
        "SELECT * FROM staff_constraints WHERE target_month=?", conn, params=(target_month,)
    )
    cross_df = pd.read_sql_query(
        "SELECT * FROM cross_month_night WHERE target_month=?", conn, params=(target_month,)
    )
    override_df = pd.read_sql_query(
        "SELECT * FROM date_overrides WHERE target_date LIKE ?", conn, params=(f"{target_month}-%",)
    )
    leave_amount_map = {row["code"]: row["leave_amount"] for _, row in pd.read_sql_query("SELECT * FROM shift_types", conn).iterrows()}
    conn.close()

    if len(staff_df) == 0:
        return False, "在籍中の職員が登録されていません。", None, []

    (_, std_nurse, std_care, allow_flex, flex_nurse, flex_care, max_consec, use_three_shift) = ns

    # 前月がこのシステムで生成済みなら自動検出、無ければ手動入力（cross_month_night）で補う
    auto_cross_staff = get_auto_cross_month_night_staff(target_month)
    manual_cross_staff = {int(crow["staff_id"]) for _, crow in cross_df.iterrows()}
    all_cross_staff = auto_cross_staff | manual_cross_staff

    work_codes = ["N", "準", "入", "am", "pm"]         # 必要人数の対象になる通常勤務
    special_codes = list(SPECIAL_CODES)                 # 出/実/研/産/育（希望があれば100%反映、必要人数の対象外）
    leave_codes = list(LEAVE_CODES)                      # 年/年am/年pm
    rest_codes = ["×", "年"] + special_codes             # 週1日休日要件のカウント対象。半休は含めない＝安全側。
    # 産休・育休・出張・研修・実習は「施設での通常勤務ではない」ため、この期間中に
    # 週1休日や連勤上限の違反が強制的に発生しないよう、休養扱いに含める。
    all_off_like = ["明", "×"] + leave_codes + special_codes  # 勤務時間としてはカウントしない区分
    all_codes = work_codes + all_off_like
    night_codes = ["入"]

    staff_ids = staff_df["id"].tolist()
    n_days = len(date_list)
    nurse_ids = staff_df[staff_df["job_type"] == "看護師"]["id"].tolist()
    care_ids = staff_df[staff_df["job_type"] == "介護士"]["id"].tolist()
    am_pm_eligible_ids = set(staff_df[staff_df["am_pm_eligible"] == 1]["id"].tolist())

    model = cp_model.CpModel()

    # shift[s][d][code] = 1 なら、職員sがd日目にcodeのシフトに入る
    shift = {}
    for s in staff_ids:
        for d in range(n_days):
            for code in all_codes:
                shift[(s, d, code)] = model.NewBoolVar(f"shift_s{s}_d{d}_{code}")

    # 各職員・各日は必ずちょうど1つのシフトが割り当てられる
    for s in staff_ids:
        for d in range(n_days):
            model.Add(sum(shift[(s, d, code)] for code in all_codes) == 1)

    # 2交代制の施設では「準夜勤」を使わない（3交代制のみで使用可能）
    if not use_three_shift:
        for s in staff_ids:
            for d in range(n_days):
                model.Add(shift[(s, d, "準")] == 0)

    # am/pmは、対応可能フラグが立っている職員以外には割り当てない
    for s in staff_ids:
        if s not in am_pm_eligible_ids:
            for d in range(n_days):
                model.Add(shift[(s, d, "am")] == 0)
                model.Add(shift[(s, d, "pm")] == 0)

    # 夜勤可否フラグ：夜勤不可の職員は「入」「準」に入らない（どちらも夜間の勤務のため）
    for _, srow in staff_df.iterrows():
        s = srow["id"]
        if not srow["night_shift_ok"]:
            for d in range(n_days):
                model.Add(shift[(s, d, "入")] == 0)
                model.Add(shift[(s, d, "準")] == 0)

    # ── ハード制約①: 施設が設定した連続勤務日数の上限（運用ルール） ──
    # 「明」は実労働ではないので、休みと同様に連勤カウントを途切れさせる扱いにする
    consec_break_codes = ["明", "×"] + leave_codes + special_codes
    for s in staff_ids:
        for d in range(n_days - max_consec):
            window = [sum(shift[(s, d + i, oc)] for oc in consec_break_codes) for i in range(max_consec + 1)]
            model.Add(sum(window) >= 1)

    # ── ハード制約②: 週1日以上の休日（労働基準法第35条・法律上の絶対ルール） ──
    # 「明」（夜勤明け）や半休は、丸1日の休養とはみなさず、独立した法定休日としてはカウントしない
    for s in staff_ids:
        for d in range(n_days - 6):
            week_off = sum(shift[(s, d + i, oc)] for i in range(7) for oc in rest_codes)
            model.Add(week_off >= 1)

    # ── ハード制約③: パート・扶養内の月間労働時間上限 ──
    conn = get_conn()
    hours_map = {row["code"]: row["hours"] for _, row in pd.read_sql_query("SELECT * FROM shift_types", conn).iterrows()}
    conn.close()
    for _, srow in staff_df.iterrows():
        s = srow["id"]
        if pd.notna(srow["monthly_hour_limit"]) and srow["monthly_hour_limit"]:
            total_minutes = []
            for d in range(n_days):
                for code in work_codes:
                    total_minutes.append(shift[(s, d, code)] * int(hours_map.get(code, 0) * 10))
            model.Add(sum(total_minutes) <= int(srow["monthly_hour_limit"] * 10))

    # ── ハード制約④: 特別区分（出張・実習・研修・産休・育休）の希望は100%反映 ──
    # これらは会社都合・制度上のものであり、個人の裁量的な希望とは性質が異なるため絶対反映とする
    request_map = {}  # (staff_id, day_index) -> requested_code
    for _, crow in cons_df.iterrows():
        s = crow["staff_id"]
        if s not in staff_ids:
            continue
        try:
            d_index = (datetime.strptime(crow["constraint_date"], "%Y-%m-%d").date() - date_list[0]).days
        except Exception:
            continue
        if d_index < 0 or d_index >= n_days:
            continue
        req_code = crow["shift_code"]
        if req_code not in all_codes:
            continue
        request_map[(s, d_index)] = req_code
        if req_code in special_codes:
            model.Add(shift[(s, d_index, req_code)] == 1)

    # 希望の無い(職員,日)には、特別区分・有給を自由に割り当てさせない
    # （希望していない人に「育」「産」などが勝手に割り振られる誤動作を防ぐ）
    restrict_codes = special_codes + leave_codes
    for s in staff_ids:
        for d in range(n_days):
            req_here = request_map.get((s, d))
            for code in restrict_codes:
                if code != req_here:
                    model.Add(shift[(s, d, code)] == 0)

    # 「明」は、①本人が明示的に希望した日、②前日が「入」だった日、③前月末が夜勤入りで
    # 月をまたいだ1日目、のいずれかでなければ割り当てない（無関係な日に「明」が
    # 勝手に連発するのを防ぐ）
    for s in staff_ids:
        for d in range(n_days):
            requested_here = 1 if request_map.get((s, d)) == "明" else 0
            if d == 0:
                cross_ok = 1 if s in all_cross_staff else 0
                model.Add(shift[(s, d, "明")] <= requested_here + cross_ok)
            else:
                model.Add(shift[(s, d, "明")] <= requested_here + shift[(s, d - 1, "入")])

    # ── ハード制約⑤: 日別・シフト別の必要人数（特別対応日 > 曜日別設定の優先順位） ──
    for d in range(n_days):
        weekday = date_list[d].weekday()
        d_iso = date_list[d].isoformat()
        day_override = override_df[override_df["target_date"] == d_iso]
        day_req = req_df[req_df["weekday"] == weekday]
        # (shift_code, job_type) -> required_count のマップを作り、特別対応日で上書きする
        need_map = {(rr["shift_code"], rr["job_type"]): int(rr["required_count"]) for _, rr in day_req.iterrows()}
        for _, orow in day_override.iterrows():
            need_map[(orow["shift_code"], orow["job_type"])] = int(orow["required_count"])
        for (code, job_type), need in need_map.items():
            if code not in work_codes or need <= 0:
                continue
            if code in ("am", "pm"):
                ids = [i for i in (nurse_ids if job_type == "看護師" else care_ids) if i in am_pm_eligible_ids]
            else:
                ids = nurse_ids if job_type == "看護師" else care_ids
            model.Add(sum(shift[(s, d, code)] for s in ids) >= need)

    # ── ハード制約⑥: 夜勤人数（標準 or 柔軟対応） ──
    for d in range(n_days):
        for code in night_codes:
            nurse_in_night = sum(shift[(s, d, code)] for s in nurse_ids)
            care_in_night = sum(shift[(s, d, code)] for s in care_ids)
            if allow_flex:
                use_standard = model.NewBoolVar(f"std_{d}_{code}")
                use_flex = model.NewBoolVar(f"flex_{d}_{code}")
                model.Add(use_standard + use_flex == 1)
                model.Add(nurse_in_night >= std_nurse).OnlyEnforceIf(use_standard)
                model.Add(care_in_night >= std_care).OnlyEnforceIf(use_standard)
                model.Add(nurse_in_night >= flex_nurse).OnlyEnforceIf(use_flex)
                model.Add(care_in_night >= flex_care).OnlyEnforceIf(use_flex)
            else:
                model.Add(nurse_in_night >= std_nurse)
                model.Add(care_in_night >= std_care)

    # ═══════════════ ここからソフト制約（できるだけ叶えたいルール） ═══════════════
    penalty_terms = []  # (weight, BoolVar or IntVar) のリスト

    # ── ソフト①: 個人の希望（休み・有給・勤務）はできるだけ反映する。矛盾する場合は「ご相談」扱い ──
    REQUEST_WEIGHT = 1000
    for (s, d_index), req_code in request_map.items():
        if req_code in special_codes:
            continue  # ハード制約側で既に100%反映済み
        # 希望が叶わなかった場合に1になる変数
        unmet = model.NewBoolVar(f"unmet_{s}_{d_index}")
        model.Add(shift[(s, d_index, req_code)] == 1).OnlyEnforceIf(unmet.Not())
        model.Add(shift[(s, d_index, req_code)] == 0).OnlyEnforceIf(unmet)
        penalty_terms.append((REQUEST_WEIGHT, unmet))

    # ── ソフト②: 夜勤入り(入)の翌日は明けを強く推奨（本人希望があればそちらを優先） ──
    NIGHT_REST_WEIGHT = 60
    for s in staff_ids:
        for d in range(n_days - 1):
            # 翌日に個人希望が指定されている場合は、その希望が優先されるため、
            # この推奨ルールのペナルティを課さない
            if (s, d + 1) in request_map:
                continue
            # 「入」だったのに翌日が「明」にならなかった場合に1になる変数
            aux = model.NewBoolVar(f"nrest_aux_{s}_{d}")
            model.Add(shift[(s, d + 1, "明")] >= shift[(s, d, "入")] - aux)
            penalty_terms.append((NIGHT_REST_WEIGHT, aux))

    # ── ソフト③: 月またぎ夜勤の翌日も明けを強く推奨（絶対ではない） ──
    for s in all_cross_staff:
        if s not in staff_ids or (s, 0) in request_map:
            continue
        aux = model.NewBoolVar(f"crossrest_aux_{s}")
        model.Add(shift[(s, 0, "明")] >= 1 - aux)
        penalty_terms.append((NIGHT_REST_WEIGHT, aux))

    # ── ソフト④: 夜勤回数の目標達成度（目標の前後1回は許容、それ以上ずれたらペナルティ） ──
    for _, srow in staff_df.iterrows():
        s = srow["id"]
        if not srow["night_shift_ok"]:
            continue
        target = int(srow["night_shift_target"] or 0)
        actual = sum(shift[(s, d, code)] for d in range(n_days) for code in night_codes)
        diff = model.NewIntVar(-31, 31, f"night_diff_{s}")
        model.Add(diff == actual - target)
        excess = model.NewIntVar(0, 31, f"night_excess_{s}")
        # |diff| が1を超えた分だけをペナルティ対象にする（前後1回は無罰）
        model.Add(excess >= diff - 1)
        model.Add(excess >= -diff - 1)
        penalty_terms.append((10, excess))

    # ── ソフト⑤: 土日勤務の偏りを減らす ──
    weekend_days = [d for d in range(n_days) if date_list[d].weekday() >= 5]
    if weekend_days:
        weekend_work_counts = []
        for s in staff_ids:
            wc = sum(shift[(s, d, code)] for d in weekend_days for code in work_codes)
            weekend_work_counts.append(wc)
        if weekend_work_counts:
            max_wc = model.NewIntVar(0, len(weekend_days), "max_weekend_work")
            min_wc = model.NewIntVar(0, len(weekend_days), "min_weekend_work")
            model.AddMaxEquality(max_wc, weekend_work_counts)
            model.AddMinEquality(min_wc, weekend_work_counts)
            weekend_spread = model.NewIntVar(0, len(weekend_days), "weekend_spread")
            model.Add(weekend_spread == max_wc - min_wc)
            penalty_terms.append((5, weekend_spread))

    # ── ソフト⑥: 「お任せ」の日は、何もしない(×)より働く方をわずかに優先する ──
    # 必要人数を0（お任せ）にした日でも、目的関数上「×」に一切ペナルティが無いと、
    # ソルバーが計算上最も簡単な「ほぼ全員休み」を選んでしまうため、
    # 個人希望が無い日については「×」に軽いペナルティを付け、通常勤務を優先させる。
    for s in staff_ids:
        for d in range(n_days):
            if (s, d) in request_map:
                continue
            penalty_terms.append((8, shift[(s, d, "×")]))

    if penalty_terms:
        model.Minimize(sum(w * v for w, v in penalty_terms))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_sec
    solver.parameters.num_search_workers = 8
    status = solver.Solve(model)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        # ── 自動診断: 夜勤の配置人数を最小(誰か1名)に緩めて再挑戦し、原因を切り分ける ──
        diag_model = cp_model.CpModel()
        diag_shift = {}
        for s in staff_ids:
            for d in range(n_days):
                for code in all_codes:
                    diag_shift[(s, d, code)] = diag_model.NewBoolVar(f"d_{s}_{d}_{code}")
        for s in staff_ids:
            for d in range(n_days):
                diag_model.Add(sum(diag_shift[(s, d, code)] for code in all_codes) == 1)
        if not use_three_shift:
            for s in staff_ids:
                for d in range(n_days):
                    diag_model.Add(diag_shift[(s, d, "準")] == 0)
        for s in staff_ids:
            if s not in am_pm_eligible_ids:
                for d in range(n_days):
                    diag_model.Add(diag_shift[(s, d, "am")] == 0)
                    diag_model.Add(diag_shift[(s, d, "pm")] == 0)
        for _, srow in staff_df.iterrows():
            s = srow["id"]
            if not srow["night_shift_ok"]:
                for d in range(n_days):
                    diag_model.Add(diag_shift[(s, d, "入")] == 0)
                    diag_model.Add(diag_shift[(s, d, "準")] == 0)
        for s in staff_ids:
            for d in range(n_days - max_consec):
                w = [sum(diag_shift[(s, d + i, oc)] for oc in consec_break_codes) for i in range(max_consec + 1)]
                diag_model.Add(sum(w) >= 1)
        for s in staff_ids:
            for d in range(n_days - 6):
                diag_model.Add(sum(diag_shift[(s, d + i, oc)] for i in range(7) for oc in rest_codes) >= 1)
        for _, srow in staff_df.iterrows():
            s = srow["id"]
            if pd.notna(srow["monthly_hour_limit"]) and srow["monthly_hour_limit"]:
                tm = [diag_shift[(s, d, code)] * int(hours_map.get(code, 0) * 10) for d in range(n_days) for code in work_codes]
                diag_model.Add(sum(tm) <= int(srow["monthly_hour_limit"] * 10))
        for (s, d_index), req_code in request_map.items():
            if req_code in special_codes:
                diag_model.Add(diag_shift[(s, d_index, req_code)] == 1)
        restrict_codes = special_codes + leave_codes
        for s in staff_ids:
            for d in range(n_days):
                req_here = request_map.get((s, d))
                for code in restrict_codes:
                    if code != req_here:
                        diag_model.Add(diag_shift[(s, d, code)] == 0)
        # 夜勤は「誰か1名以上いればOK」まで緩めた最小版
        for d in range(n_days):
            diag_model.Add(sum(diag_shift[(s, d, "入")] for s in staff_ids) >= 1)
        diag_solver = cp_model.CpSolver()
        diag_solver.parameters.max_time_in_seconds = min(15, time_limit_sec)
        diag_status = diag_solver.Solve(diag_model)

        if diag_status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            return False, (
                "⚠️ 原因が絞れました: 「⚙️シフト・条件設定 → 🌙夜勤の配置ルール」タブの"
                "**標準人数（看護師○名＋介護士○名を毎日必ず確保）が厳しすぎます。**"
                "曜日別の最低人数指定を0にしても、この夜勤の人数設定は別枠で毎日効いています。"
                "この人数を減らすか、対応可能な夜勤スタッフを増やしてください。"
            ), None, []
        else:
            return False, (
                "絶対に守るべき条件（週1日休日、月間労働時間上限、希望・特別区分の反映など）だけでも"
                "満たすシフトが見つかりませんでした。職員数が少なすぎるか、月間労働時間上限や"
                "希望休みが厳しすぎる可能性があります。まずは職員を増やすか、条件を緩めてお試しください。"
            ), None, []

    # 結果をDataFrameに整形（縦軸: 職員名、横軸: 日付）
    result_rows = []
    name_map = dict(zip(staff_df["id"], staff_df["name"]))
    job_map = dict(zip(staff_df["id"], staff_df["job_type"]))
    assigned_map = {}  # (s, d) -> code
    for s in staff_ids:
        row = {"職員名": name_map[s], "職種": job_map[s]}
        for d in range(n_days):
            for code in all_codes:
                if solver.Value(shift[(s, d, code)]) == 1:
                    row[f"{date_list[d].day}日({WEEKDAY_JP[date_list[d].weekday()]})"] = code
                    assigned_map[(s, d)] = code
                    break
        result_rows.append(row)
    result_df = pd.DataFrame(result_rows)

    # ── 日別の集計行を末尾に追加する ──
    date_col_names = [f"{date_list[d].day}日({WEEKDAY_JP[date_list[d].weekday()]})" for d in range(n_days)]
    summary_defs = [
        ("日勤(看)", "N", "看護師"), ("日勤(介)", "N", "介護士"),
        ("入り(看)", "入", "看護師"), ("明け(看)", "明", "看護師"),
        ("入り(介)", "入", "介護士"), ("明け(介)", "明", "介護士"),
        ("休み", "×", None),
    ]
    summary_rows = []
    for label, target_code, target_job in summary_defs:
        srow = {"職員名": f"▼{label}", "職種": ""}
        for d in range(n_days):
            cnt = 0
            for s in staff_ids:
                if assigned_map.get((s, d)) != target_code:
                    continue
                if target_job is not None and job_map[s] != target_job:
                    continue
                cnt += 1
            srow[date_col_names[d]] = cnt
        summary_rows.append(srow)
    result_df = pd.concat([result_df, pd.DataFrame(summary_rows)], ignore_index=True)

    # ── 「ご相談」判定: 個人希望と実際の割り当てが食い違った箇所を洗い出す ──
    consult_list = []
    for (s, d_index), req_code in request_map.items():
        actual_code = assigned_map.get((s, d_index), "")
        if actual_code != req_code:
            consult_list.append({
                "職員名": name_map[s],
                "日付": f"{date_list[d_index].month}/{date_list[d_index].day}({WEEKDAY_JP[date_list[d_index].weekday()]})",
                "希望していた内容": req_code,
                "実際の割り当て（代替案）": actual_code,
            })

    # ── DBへ保存（既存の当月データは一旦削除してから登録） ──
    conn = get_conn()
    conn.execute("DELETE FROM generated_shifts WHERE target_month=?", (target_month,))
    for (s, d), code in assigned_map.items():
        conn.execute(
            "INSERT INTO generated_shifts (staff_id, target_month, work_date, shift_code) VALUES (?,?,?,?)",
            (s, target_month, date_list[d].isoformat(), code),
        )
    conn.commit()

    # ── 有給の自動消化記録（年・年am・年pmが割り当てられた職員分を自動記録） ──
    # 同じ月・同じ内容の自動記録が既にある場合は重複させないよう、まず当月分の自動記録を削除してから作り直す
    conn.execute(
        "DELETE FROM paid_leave_usage WHERE source='自動' AND used_date LIKE ?",
        (f"{target_month}-%",),
    )
    for (s, d), code in assigned_map.items():
        amt = leave_amount_map.get(code, 0)
        if amt and amt > 0:
            conn.execute(
                "INSERT INTO paid_leave_usage (staff_id, used_date, amount, source, note) VALUES (?,?,?,?,?)",
                (s, date_list[d].isoformat(), amt, "自動", "シフト自動生成による自動記録"),
            )
    conn.commit()
    conn.close()

    status_msg = "最適解が見つかりました。" if status == cp_model.OPTIMAL else "実行可能な解が見つかりました（時間内での最善解）。"
    if consult_list:
        status_msg += f" ただし、{len(consult_list)}件、希望通りにならなかった箇所があります（下の「ご相談」一覧をご確認ください）。"
    return True, status_msg, result_df, consult_list

def build_excel_export(result_df: pd.DataFrame, target_month: str) -> bytes:
    """シフト結果を、集計列付きのExcelバイト列として返す（末尾の日別集計行は個人集計の対象外にする）"""
    conn = get_conn()
    hours_map = {row["code"]: row["hours"] for _, row in pd.read_sql_query("SELECT * FROM shift_types", conn).iterrows()}
    conn.close()

    day_cols = [c for c in result_df.columns if c not in ("職員名", "職種")]

    is_summary_row = result_df["職員名"].astype(str).str.startswith("▼")
    staff_only_df = result_df[~is_summary_row].copy()
    summary_only_df = result_df[is_summary_row].copy()

    day_counts, night_counts, paid_counts, total_hours = [], [], [], []
    for _, row in staff_only_df.iterrows():
        codes = [row[c] for c in day_cols]
        day_counts.append(sum(1 for c in codes if c == "N"))
        night_counts.append(sum(1 for c in codes if c == "入"))
        paid_counts.append(sum(1 for c in codes if c == "年") + sum(0.5 for c in codes if c in ("年am", "年pm")))
        total_hours.append(round(sum(hours_map.get(c, 0) for c in codes), 1))

    staff_only_df["日勤回数"] = day_counts
    staff_only_df["夜勤回数"] = night_counts
    staff_only_df["有休回数"] = paid_counts
    staff_only_df["合計労働時間"] = total_hours

    # 集計行は、集計列を空欄のまま末尾に追加する
    for col in ["日勤回数", "夜勤回数", "有休回数", "合計労働時間"]:
        summary_only_df[col] = ""
    export_df = pd.concat([staff_only_df, summary_only_df], ignore_index=True)

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        export_df.to_excel(writer, index=False, sheet_name=target_month)
        ws = writer.sheets[target_month]
        # 列幅を少し調整して見やすくする
        for i, col in enumerate(export_df.columns, start=1):
            ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = max(8, len(str(col)) + 2)
    buf.seek(0)
    return buf.getvalue()


# ═════════════════════════════════════════════
# TAB 4: シフト自動生成
# ═════════════════════════════════════════════
with tabs[4]:
    st.subheader("🤖 シフト自動生成")

    if not ORTOOLS_AVAILABLE:
        st.error("OR-Tools がインストールされていません。`pip install ortools` を実行してください。")
    else:
        st.markdown("""
        **このエンジンが厳格に守る「絶対ルール」**
        - 週1日以上の休日は必ず確保する（労働基準法第35条・絶対ルール）
        - パート・扶養内の職員は、設定した月間労働時間の上限を超えない
        - 施設で設定した連続勤務日数を超えない
        - 出張・実習・研修・産休・育休の希望は100%反映する
        - 介護士が不足する夜勤は、設定に応じて看護師で自動補填する
        - 午前(am)・午後(pm)勤務は、対応可能と設定した職員にのみ割り当てる

        **できるだけ叶えようとする「努力目標」（マンパワー不足の場合は柔軟に調整）**
        - 希望休み・有給希望・希望勤務は、できる限り反映する（強い優先度）
        - 夜勤入り(入)の翌日は明け(明)にすることを推奨するが、本人希望や人手不足の状況次第では
          夜勤の連続や、夜勤明けすぐの日勤なども許容する（絶対には縛らない）
        - 夜勤可能な職員の間で、夜勤回数が目標回数の前後1回以内に収まるようにする
        - 土日の勤務日数が特定の職員に偏らないようにする

        希望が重なって両立できない場合は、生成結果と一緒に「ご相談」一覧に表示し、
        実際にどう割り当てたか（代替案）も分かるようにします。
        """)

        gen_month = st.text_input("生成する月 (YYYY-MM)", value=date.today().strftime("%Y-%m"))
        time_limit = st.slider("計算の最大時間（秒）", min_value=10, max_value=120, value=30,
                                help="職員数・日数が多いほど時間がかかります。見つからない場合は時間を延ばしてみてください。")

        if st.button("🚀 この条件でシフトを自動生成する", type="primary"):
            with st.spinner("計算中です。しばらくお待ちください…"):
                success, message, result_df, consult_list = build_and_solve_schedule(gen_month, time_limit)
            if success:
                st.success(message)
                st.session_state["last_result_df"] = result_df
                st.session_state["last_result_month"] = gen_month
                st.session_state["last_consult_list"] = consult_list
            else:
                st.error(message)

        if "last_result_df" in st.session_state:
            st.divider()
            st.markdown(f"### 📋 {st.session_state['last_result_month']} のシフト表")
            result_col_config = {"職員名": st.column_config.TextColumn("職員名", width="medium")}
            st.dataframe(st.session_state["last_result_df"], use_container_width=True, hide_index=True,
                         height=600, column_config=result_col_config)

            consult_list = st.session_state.get("last_consult_list", [])
            if consult_list:
                st.divider()
                st.markdown("### 💬 ご相談（希望通りにならなかった箇所）")
                st.caption(
                    "人手の都合上、以下の希望はそのまま反映できませんでした。"
                    "「実際の割り当て（代替案）」の内容でよいか、本人と相談のうえご確認ください。"
                )
                st.dataframe(pd.DataFrame(consult_list), use_container_width=True, hide_index=True)

            excel_bytes = build_excel_export(st.session_state["last_result_df"], st.session_state["last_result_month"])
            st.download_button(
                label="📥 Excel形式でダウンロード",
                data=excel_bytes,
                file_name=f"シフト表_{st.session_state['last_result_month']}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )

# ═════════════════════════════════════════════
# TAB (新設): バックアップ（PC・タブレットへの保存と復元）
# ═════════════════════════════════════════════
with tabs[5]:
    st.subheader("💾 データのバックアップ")
    st.warning(
        "⚠️ **このアプリのデータは、Streamlit Cloud上に保存されています。**\n\n"
        "GitHubのコードを更新して再デプロイされたタイミングなどで、まれにデータが"
        "リセットされてしまうことがあります。**大事な操作（月初の登録作業が終わった時など）の後は、"
        "こまめにバックアップをPC・タブレットに保存しておくことを強くおすすめします。**"
    )

    st.markdown("### 📥 バックアップを保存する")
    st.caption(
        "職員情報・シフト希望・有給記録・生成済みシフト表・各種設定など、すべてのデータを"
        "1つのファイルにまとめてダウンロードできます。PC・タブレットの分かりやすい場所に保存してください。"
    )
    try:
        with open(DB_PATH, "rb") as f:
            db_bytes = f.read()
        backup_filename = f"shift_app_backup_{date.today().isoformat()}.db"
        st.download_button(
            label="💾 今すぐバックアップをダウンロード",
            data=db_bytes,
            file_name=backup_filename,
            mime="application/octet-stream",
            type="primary",
        )
        st.caption(f"現在のデータサイズ: 約{len(db_bytes) / 1024:.1f} KB")
    except FileNotFoundError:
        st.info("まだデータがありません。")

    st.divider()

    st.markdown("### 📤 バックアップから復元する")
    st.error(
        "⚠️ **復元すると、今アプリに入っている内容はすべて上書きされます。** "
        "本当に必要な時だけお使いください。"
    )
    uploaded_file = st.file_uploader(
        "バックアップファイル（.db）を選んでください",
        type=["db"],
        key="restore_uploader",
    )
    if uploaded_file is not None:
        st.write(f"選択されたファイル: {uploaded_file.name}（{uploaded_file.size / 1024:.1f} KB）")
        confirm_restore = st.checkbox("内容を確認しました。このファイルで今のデータを上書きします。")
        if confirm_restore and st.button("🔄 このバックアップで復元する", type="primary"):
            try:
                with open(DB_PATH, "wb") as f:
                    f.write(uploaded_file.getbuffer())
                st.success("復元が完了しました。画面を再読み込みします…")
                st.rerun()
            except Exception as e:
                st.error(f"復元中にエラーが発生しました: {e}")

    st.divider()
    st.markdown("### 🕐 自動バックアップのおすすめタイミング")
    st.markdown("""
    - 新しい月のシフト希望をすべて入力し終えたとき
    - シフトを自動生成して、内容を確定したとき
    - 職員の入退社などマスター情報を変更したとき
    - GitHubのコードを更新する前（念のため）
    """)
