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
    # code, name, start, end, hours, is_night(夜勤扱いか)
    ("D",  "日勤",   "08:30", "17:30", 8.0, False),
    ("N1", "準夜勤", "16:30", "01:00", 8.0, True),
    ("N2", "深夜勤", "00:30", "09:00", 8.0, True),
    ("E",  "早出",   "07:00", "16:00", 8.0, False),
    ("AM", "午前勤務", "08:30", "12:30", 4.0, False),
    ("PM", "午後勤務", "13:30", "17:30", 4.0, False),
    ("休", "休み",   "", "", 0.0, False),
    ("有", "有給",   "", "", 0.0, False),
]

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
            hire_date TEXT NOT NULL,          -- 入社年月日 YYYY-MM-DD
            active INTEGER NOT NULL DEFAULT 1,
            note TEXT
        )
    """)

    # 職員ごとのシフト制約（希望休み・希望勤務・時間縛り等）月単位で管理
    c.execute("""
        CREATE TABLE IF NOT EXISTS staff_constraints (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            staff_id INTEGER NOT NULL,
            target_month TEXT NOT NULL,   -- YYYY-MM
            constraint_date TEXT NOT NULL, -- YYYY-MM-DD
            constraint_type TEXT NOT NULL, -- 希望休み / 希望勤務 / 有給希望
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
            note TEXT,
            FOREIGN KEY (staff_id) REFERENCES staff(id) ON DELETE CASCADE
        )
    """)

    # シフト区分マスター
    c.execute("""
        CREATE TABLE IF NOT EXISTS shift_types (
            code TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            start_time TEXT,
            end_time TEXT,
            hours REAL NOT NULL,
            is_night INTEGER NOT NULL DEFAULT 0
        )
    """)

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

    # 夜勤の柔軟配置設定（施設全体で1つ）
    c.execute("""
        CREATE TABLE IF NOT EXISTS night_shift_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            standard_nurse INTEGER NOT NULL DEFAULT 1,
            standard_care INTEGER NOT NULL DEFAULT 2,
            allow_flex INTEGER NOT NULL DEFAULT 1,   -- 介護士不足時、看護師で補填してよいか
            flex_nurse INTEGER NOT NULL DEFAULT 2,
            flex_care INTEGER NOT NULL DEFAULT 1,
            max_consecutive_days INTEGER NOT NULL DEFAULT 4  -- 施設運用ルール（法律の絶対上限ではない）
        )
    """)
    c.execute("INSERT OR IGNORE INTO night_shift_settings (id) VALUES (1)")

    # 月またぎ夜勤（前月最終日が夜勤で、当月1日への影響がある場合の入力）
    c.execute("""
        CREATE TABLE IF NOT EXISTS cross_month_night (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            staff_id INTEGER NOT NULL,
            target_month TEXT NOT NULL,  -- YYYY-MM（当月）
            prev_month_last_shift TEXT,  -- 前月末日の勤務コード（例: N2）
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
        for code, name, start, end, hours, is_night in DEFAULT_SHIFT_TYPES:
            c.execute(
                "INSERT INTO shift_types (code, name, start_time, end_time, hours, is_night) VALUES (?,?,?,?,?,?)",
                (code, name, start, end, hours, int(is_night)),
            )
        conn.commit()

    # 曜日別必要人数の初期データ投入（全曜日: 日勤 看護師2/介護士3、早出 看護師1/介護士1、夜勤は別テーブルで管理）
    c.execute("SELECT COUNT(*) FROM staffing_requirements")
    if c.fetchone()[0] == 0:
        default_req = {
            "D": {"看護師": 2, "介護士": 3},
            "E": {"看護師": 1, "介護士": 1},
            "AM": {"看護師": 0, "介護士": 1},
            "PM": {"看護師": 0, "介護士": 1},
        }
        for weekday in range(7):
            for shift_code, jobs in default_req.items():
                for job_type, cnt in jobs.items():
                    # 入浴日は火・木・土を初期値としておく（施設ごとに変更可能）
                    is_bath = 1 if weekday in (1, 3, 5) and shift_code in ("D", "E") else 0
                    extra = 1 if is_bath and job_type == "介護士" else 0
                    c.execute(
                        "INSERT OR IGNORE INTO staffing_requirements (weekday, shift_code, job_type, required_count, is_bath_day) VALUES (?,?,?,?,?)",
                        (weekday, shift_code, job_type, cnt + extra, is_bath),
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


def calc_paid_leave_balance(staff_id: int, hire_date_str: str) -> dict:
    """付与日数・消化日数・残日数をまとめて返す"""
    granted = calc_paid_leave_grant_days(hire_date_str)
    conn = get_conn()
    used = conn.execute(
        "SELECT COUNT(*) FROM paid_leave_usage WHERE staff_id = ?", (staff_id,)
    ).fetchone()[0]
    conn.close()
    return {"granted": granted, "used": used, "remaining": max(0, granted - used)}


# ─────────────────────────────────────────────
# Streamlit 画面
# ─────────────────────────────────────────────
st.set_page_config(page_title="勤務表自動生成システム", layout="wide")
init_db()
seed_sample_staff()

st.title("🗓️ 勤務表自動生成システム")
st.caption("看護師・介護士向け シフト管理ツール（試作版）")

tab_names = ["👥 職員登録・一覧", "🌴 有給管理", "⚙️ シフト・条件設定", "🤖 シフト自動生成"]
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
                f_hire = st.date_input("入社年月日 *", value=date.today())
            with col2:
                f_limit = None
                if f_emp in ("パート", "扶養内"):
                    f_limit = st.number_input("月間労働時間上限（時間）", min_value=1, max_value=200, value=80)
                f_night_ok = st.checkbox("夜勤可能", value=True)
                f_night_target = st.number_input("月間目標夜勤回数", min_value=0, max_value=15, value=4)
                f_note = st.text_input("備考（任意）")

            submitted = st.form_submit_button("登録する", type="primary")
            if submitted:
                if not f_name.strip():
                    st.error("職員名を入力してください。")
                else:
                    conn = get_conn()
                    conn.execute(
                        """INSERT INTO staff (name, job_type, employment_type, monthly_hour_limit,
                           night_shift_ok, night_shift_target, hire_date, active, note)
                           VALUES (?,?,?,?,?,?,?,?,?)""",
                        (f_name.strip(), f_job, f_emp, f_limit, int(f_night_ok),
                         f_night_target, f_hire.isoformat(), 1, f_note),
                    )
                    conn.commit()
                    conn.close()
                    st.success(f"{f_name} さんを登録しました。")
                    st.rerun()

    st.divider()
    st.subheader("📋 職員一覧")

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
        for _, row in view_df.iterrows():
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
                                            key=f"hire_{row['id']}")
                with ec2:
                    e_limit = row["monthly_hour_limit"] if row["monthly_hour_limit"] else 80
                    if e_emp in ("パート", "扶養内"):
                        e_limit = st.number_input("月間労働時間上限（時間）", min_value=1, max_value=200,
                                                    value=int(e_limit), key=f"limit_{row['id']}")
                    else:
                        e_limit = None
                        st.caption("常勤のため時間上限なし")
                    e_night_ok = st.checkbox("夜勤可能", value=bool(row["night_shift_ok"]), key=f"nok_{row['id']}")
                    e_night_target = st.number_input("月間目標夜勤回数", min_value=0, max_value=15,
                                                       value=int(row["night_shift_target"] or 0), key=f"ntgt_{row['id']}")
                    e_active = st.checkbox("在籍中", value=bool(row["active"]), key=f"active_{row['id']}")
                e_note = st.text_input("備考", value=row["note"] or "", key=f"note_{row['id']}")

                bcol1, bcol2 = st.columns([1, 1])
                with bcol1:
                    if st.button("💾 更新を保存", key=f"save_{row['id']}"):
                        conn = get_conn()
                        conn.execute(
                            """UPDATE staff SET name=?, job_type=?, employment_type=?, monthly_hour_limit=?,
                               night_shift_ok=?, night_shift_target=?, hire_date=?, active=?, note=?
                               WHERE id=?""",
                            (e_name, e_job, e_emp, e_limit, int(e_night_ok), e_night_target,
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

                # ── この職員の希望休み・希望勤務の登録 ──
                st.markdown("**📌 シフト希望（希望休み・希望勤務・有給希望）**")
                wc1, wc2, wc3 = st.columns(3)
                with wc1:
                    w_month = st.text_input("対象月 (YYYY-MM)", value=date.today().strftime("%Y-%m"),
                                             key=f"wmonth_{row['id']}")
                with wc2:
                    w_date = st.date_input("希望日", value=date.today(), key=f"wdate_{row['id']}")
                with wc3:
                    w_type = st.selectbox("種別", ["希望休み", "有給希望", "希望勤務"], key=f"wtype_{row['id']}")
                w_shift_code = ""
                if w_type == "希望勤務":
                    w_shift_code = st.selectbox("希望するシフト", [s[0] for s in DEFAULT_SHIFT_TYPES if s[0] not in ("休", "有")],
                                                 key=f"wshift_{row['id']}")
                if st.button("この希望を追加", key=f"waddbtn_{row['id']}"):
                    conn = get_conn()
                    conn.execute(
                        """INSERT INTO staff_constraints (staff_id, target_month, constraint_date,
                           constraint_type, shift_code, memo) VALUES (?,?,?,?,?,?)""",
                        (row["id"], w_month, w_date.isoformat(), w_type, w_shift_code, ""),
                    )
                    conn.commit()
                    conn.close()
                    st.success("希望を登録しました。")
                    st.rerun()

                conn = get_conn()
                cons_df = pd.read_sql_query(
                    "SELECT id, target_month, constraint_date, constraint_type, shift_code FROM staff_constraints WHERE staff_id=? ORDER BY constraint_date",
                    conn, params=(row["id"],),
                )
                conn.close()
                if len(cons_df):
                    st.dataframe(cons_df, use_container_width=True, hide_index=True)
                    del_id = st.selectbox("削除する希望のID", [0] + cons_df["id"].tolist(), key=f"delcons_{row['id']}")
                    if del_id and st.button("選択した希望を削除", key=f"delconsbtn_{row['id']}"):
                        conn = get_conn()
                        conn.execute("DELETE FROM staff_constraints WHERE id=?", (del_id,))
                        conn.commit()
                        conn.close()
                        st.rerun()

# ═════════════════════════════════════════════
# TAB 2: 有給管理
# ═════════════════════════════════════════════
with tabs[1]:
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

        st.divider()
        st.markdown("**✅ 有給消化の記録を追加する**")
        lc1, lc2, lc3 = st.columns(3)
        with lc1:
            l_staff = st.selectbox("職員を選択", staff_df["name"].tolist(), key="leave_staff_select")
        with lc2:
            l_date = st.date_input("取得日", value=date.today(), key="leave_date_input")
        with lc3:
            l_note = st.text_input("備考（任意）", key="leave_note_input")

        if st.button("この日を有給消化として記録", type="primary"):
            staff_id = int(staff_df[staff_df["name"] == l_staff]["id"].iloc[0])
            conn = get_conn()
            conn.execute(
                "INSERT INTO paid_leave_usage (staff_id, used_date, note) VALUES (?,?,?)",
                (staff_id, l_date.isoformat(), l_note),
            )
            conn.commit()
            conn.close()
            st.success(f"{l_staff} さんの有給消化（{l_date}）を記録しました。")
            st.rerun()

        with st.expander("🗑️ 有給消化記録の削除"):
            conn = get_conn()
            usage_df = pd.read_sql_query(
                """SELECT plu.id, s.name as 職員名, plu.used_date as 取得日, plu.note as 備考
                   FROM paid_leave_usage plu JOIN staff s ON plu.staff_id = s.id
                   ORDER BY plu.used_date DESC""",
                conn,
            )
            conn.close()
            if len(usage_df):
                st.dataframe(usage_df, use_container_width=True, hide_index=True)
                del_usage_id = st.selectbox("削除する記録のID", [0] + usage_df["id"].tolist())
                if del_usage_id and st.button("この記録を削除"):
                    conn = get_conn()
                    conn.execute("DELETE FROM paid_leave_usage WHERE id=?", (del_usage_id,))
                    conn.commit()
                    conn.close()
                    st.rerun()
            else:
                st.caption("消化記録はまだありません。")

# ═════════════════════════════════════════════
# TAB 3: シフト・条件設定
# ═════════════════════════════════════════════
with tabs[2]:
    st.subheader("⚙️ シフト区分・必要人数・夜勤ルールの設定")

    sub_tab1, sub_tab2, sub_tab3, sub_tab4 = st.tabs(
        ["🕐 シフト区分", "📅 曜日別必要人数", "🌙 夜勤の配置ルール", "🔀 月またぎ夜勤"]
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

    # ── 曜日別必要人数 ──
    with sub_tab2:
        st.caption(
            "曜日ごと・シフト区分ごとに必要な人数を設定します。「入浴日」等、特定曜日で人数が増える場合は"
            "「特別対応日」にチェックを入れてください（介護士+1名など、施設の実情に合わせて数値を調整してください）。"
        )
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

    # ── 夜勤の配置ルール ──
    with sub_tab3:
        conn = get_conn()
        ns = conn.execute("SELECT * FROM night_shift_settings WHERE id=1").fetchone()
        conn.close()
        # (id, standard_nurse, standard_care, allow_flex, flex_nurse, flex_care, max_consecutive_days)

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
                   flex_nurse=?, flex_care=?, max_consecutive_days=? WHERE id=1""",
                (std_nurse, std_care, int(allow_flex), flex_nurse, flex_care, max_consec),
            )
            conn.commit()
            conn.close()
            st.success("保存しました。")

    # ── 月またぎ夜勤 ──
    with sub_tab4:
        st.caption(
            "労基法上、夜勤のような日をまたぐ勤務は「始業時刻が属する日の勤務」として1回の連続勤務でカウントします。"
            "前月末日が夜勤だった職員がいる場合、当月の生成に反映するためここに入力してください。"
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
            cm_shift = st.selectbox("前月末日の勤務コード", ["N1", "N2"], key="cm_shift")

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
    戻り値: (success: bool, message: str, result_df: DataFrame or None)
    """
    if not ORTOOLS_AVAILABLE:
        return False, "OR-Tools がインストールされていません。requirements.txt を確認してください。", None

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
    conn.close()

    if len(staff_df) == 0:
        return False, "在籍中の職員が登録されていません。", None

    (_, std_nurse, std_care, allow_flex, flex_nurse, flex_care, max_consec) = ns

    work_codes = ["D", "N1", "N2", "E", "AM", "PM"]  # 「休」「有」は非勤務扱いとして別管理
    off_codes = ["休", "有"]
    all_codes = work_codes + off_codes
    night_codes = ["N1", "N2"]

    staff_ids = staff_df["id"].tolist()
    n_days = len(date_list)

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

    # 夜勤可否フラグ：夜勤不可の職員は夜勤に入らない
    for _, srow in staff_df.iterrows():
        s = srow["id"]
        if not srow["night_shift_ok"]:
            for d in range(n_days):
                for code in night_codes:
                    model.Add(shift[(s, d, code)] == 0)

    # ── ハード制約①: 夜勤の翌日は休み（安全ルール） ──
    for s in staff_ids:
        for d in range(n_days - 1):
            for code in night_codes:
                # 夜勤に入った場合、翌日は「休」または「有」のみ許可
                model.Add(
                    sum(shift[(s, d + 1, oc)] for oc in off_codes) >= shift[(s, d, code)]
                )

    # ── ハード制約②: 月またぎ夜勤（前月末が夜勤なら当月1日は休み） ──
    cross_staff_ids = set()
    for _, crow in cross_df.iterrows():
        s = crow["staff_id"]
        if s in staff_ids:
            cross_staff_ids.add(s)
            model.Add(sum(shift[(s, 0, oc)] for oc in off_codes) == 1)

    # ── ハード制約③: 連続勤務日数の上限（施設運用ルール） ──
    for s in staff_ids:
        for d in range(n_days - max_consec):
            window = [sum(shift[(s, d + i, oc)] for oc in off_codes) for i in range(max_consec + 1)]
            # max_consec+1日間の窓の中に、休み系が最低1日は含まれること
            model.Add(sum(window) >= 1)

    # ── ハード制約④: 週1日以上の休日（労働基準法第35条・法律上の絶対ルール） ──
    # 7日間の移動窓ごとに、休み系（休 or 有）が最低1日含まれること
    for s in staff_ids:
        for d in range(n_days - 6):
            week_off = sum(shift[(s, d + i, oc)] for i in range(7) for oc in off_codes)
            model.Add(week_off >= 1)

    # ── ハード制約⑤: パート・扶養内の月間労働時間上限 ──
    conn = get_conn()
    hours_map = {row["code"]: row["hours"] for _, row in pd.read_sql_query("SELECT * FROM shift_types", conn).iterrows()}
    conn.close()
    for _, srow in staff_df.iterrows():
        s = srow["id"]
        if srow["monthly_hour_limit"]:
            total_minutes = []
            for d in range(n_days):
                for code in work_codes:
                    # 時間を10倍した整数で扱う（CP-SATは整数制約のため）
                    total_minutes.append(shift[(s, d, code)] * int(hours_map.get(code, 0) * 10))
            model.Add(sum(total_minutes) <= int(srow["monthly_hour_limit"] * 10))

    # ── ハード制約⑥: 個人希望・有給の反映 ──
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
        if crow["constraint_type"] == "希望休み":
            model.Add(shift[(s, d_index, "休")] == 1)
        elif crow["constraint_type"] == "有給希望":
            model.Add(shift[(s, d_index, "有")] == 1)
        elif crow["constraint_type"] == "希望勤務" and crow["shift_code"] in work_codes:
            model.Add(shift[(s, d_index, crow["shift_code"])] == 1)

    # ── ハード制約⑦: 日別・シフト別の必要人数（日勤系は職種ごとに厳密に満たす） ──
    nurse_ids = staff_df[staff_df["job_type"] == "看護師"]["id"].tolist()
    care_ids = staff_df[staff_df["job_type"] == "介護士"]["id"].tolist()

    for d in range(n_days):
        weekday = date_list[d].weekday()  # 0=月
        day_req = req_df[req_df["weekday"] == weekday]
        for _, rr in day_req.iterrows():
            code = rr["shift_code"]
            job_type = rr["job_type"]
            need = int(rr["required_count"])
            ids = nurse_ids if job_type == "看護師" else care_ids
            if need > 0 and code in work_codes:
                model.Add(sum(shift[(s, d, code)] for s in ids) >= need)

    # ── ハード制約⑧: 夜勤人数（標準 or 柔軟対応） ──
    for d in range(n_days):
        for code in night_codes:
            nurse_in_night = sum(shift[(s, d, code)] for s in nurse_ids)
            care_in_night = sum(shift[(s, d, code)] for s in care_ids)
            if allow_flex:
                # 「標準構成」または「柔軟構成」のどちらかを満たせばよい、という制約は
                # CP-SATでは論理OR（ブール変数で分岐）として表現する
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

    # ── ソフト制約: 夜勤回数の目標達成度（平準化） ──
    penalty_terms = []
    for _, srow in staff_df.iterrows():
        s = srow["id"]
        if not srow["night_shift_ok"]:
            continue
        target = int(srow["night_shift_target"] or 0)
        actual = sum(shift[(s, d, code)] for d in range(n_days) for code in night_codes)
        diff = model.NewIntVar(-31, 31, f"night_diff_{s}")
        model.Add(diff == actual - target)
        abs_diff = model.NewIntVar(0, 31, f"night_absdiff_{s}")
        model.AddAbsEquality(abs_diff, diff)
        penalty_terms.append(abs_diff)

    # ── ソフト制約: 土日休みの偏りを減らす（土日勤務日数のばらつきを抑える） ──
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
            penalty_terms.append(weekend_spread)

    if penalty_terms:
        model.Minimize(sum(penalty_terms))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_sec
    solver.parameters.num_search_workers = 8
    status = solver.Solve(model)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return False, (
            "条件を満たすシフトが見つかりませんでした。必要人数・希望休みの数・"
            "月間上限時間などが厳しすぎる可能性があります。設定を見直してください。"
        ), None

    # 結果をDataFrameに整形（縦軸: 職員名、横軸: 日付）
    result_rows = []
    name_map = dict(zip(staff_df["id"], staff_df["name"]))
    job_map = dict(zip(staff_df["id"], staff_df["job_type"]))
    for s in staff_ids:
        row = {"職員名": name_map[s], "職種": job_map[s]}
        for d in range(n_days):
            for code in all_codes:
                if solver.Value(shift[(s, d, code)]) == 1:
                    row[f"{date_list[d].day}日({WEEKDAY_JP[date_list[d].weekday()]})"] = code
                    break
        result_rows.append(row)
    result_df = pd.DataFrame(result_rows)

    # DBへ保存（既存の当月データは一旦削除してから登録）
    conn = get_conn()
    conn.execute("DELETE FROM generated_shifts WHERE target_month=?", (target_month,))
    for s in staff_ids:
        for d in range(n_days):
            for code in all_codes:
                if solver.Value(shift[(s, d, code)]) == 1:
                    conn.execute(
                        "INSERT INTO generated_shifts (staff_id, target_month, work_date, shift_code) VALUES (?,?,?,?)",
                        (s, target_month, date_list[d].isoformat(), code),
                    )
                    break
    conn.commit()
    conn.close()

    status_msg = "最適解が見つかりました。" if status == cp_model.OPTIMAL else "実行可能な解が見つかりました（時間内での最善解）。"
    return True, status_msg, result_df


def build_excel_export(result_df: pd.DataFrame, target_month: str) -> bytes:
    """シフト結果を、集計列付きのExcelバイト列として返す"""
    conn = get_conn()
    hours_map = {row["code"]: row["hours"] for _, row in pd.read_sql_query("SELECT * FROM shift_types", conn).iterrows()}
    conn.close()

    day_cols = [c for c in result_df.columns if c not in ("職員名", "職種")]

    export_df = result_df.copy()
    day_counts, night_counts, paid_counts, total_hours = [], [], [], []
    for _, row in export_df.iterrows():
        codes = [row[c] for c in day_cols]
        day_counts.append(sum(1 for c in codes if c == "D"))
        night_counts.append(sum(1 for c in codes if c in ("N1", "N2")))
        paid_counts.append(sum(1 for c in codes if c == "有"))
        total_hours.append(round(sum(hours_map.get(c, 0) for c in codes), 1))

    export_df["日勤回数"] = day_counts
    export_df["夜勤回数"] = night_counts
    export_df["有休回数"] = paid_counts
    export_df["合計労働時間"] = total_hours

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
with tabs[3]:
    st.subheader("🤖 シフト自動生成")

    if not ORTOOLS_AVAILABLE:
        st.error("OR-Tools がインストールされていません。`pip install ortools` を実行してください。")
    else:
        st.markdown("""
        **このエンジンが厳格に守る「絶対ルール」**
        - 夜勤の翌日は必ず休み（安全のための施設ルール）
        - 前月末が夜勤だった職員は、当月1日を必ず休みにする（月またぎ夜勤の考慮）
        - 施設で設定した連続勤務日数を超えない
        - **7日間のうち必ず1日以上の休日を確保する（労働基準法第35条）**
        - パート・扶養内の職員は、設定した月間労働時間の上限を超えない
        - 登録済みの希望休み・有給希望・希望勤務は100%反映する
        - 介護士が不足する夜勤は、設定に応じて看護師で自動補填する

        **できるだけ叶えようとする「努力目標」**
        - 夜勤可能な職員の間で、夜勤回数が目標回数に近くなるようにする
        - 土日の勤務日数が特定の職員に偏らないようにする
        """)

        gen_month = st.text_input("生成する月 (YYYY-MM)", value=date.today().strftime("%Y-%m"))
        time_limit = st.slider("計算の最大時間（秒）", min_value=10, max_value=120, value=30,
                                help="職員数・日数が多いほど時間がかかります。見つからない場合は時間を延ばしてみてください。")

        if st.button("🚀 この条件でシフトを自動生成する", type="primary"):
            with st.spinner("計算中です。しばらくお待ちください…"):
                success, message, result_df = build_and_solve_schedule(gen_month, time_limit)
            if success:
                st.success(message)
                st.session_state["last_result_df"] = result_df
                st.session_state["last_result_month"] = gen_month
            else:
                st.error(message)

        if "last_result_df" in st.session_state:
            st.divider()
            st.markdown(f"### 📋 {st.session_state['last_result_month']} のシフト表")
            st.dataframe(st.session_state["last_result_df"], use_container_width=True, hide_index=True, height=600)

            excel_bytes = build_excel_export(st.session_state["last_result_df"], st.session_state["last_result_month"])
            st.download_button(
                label="📥 Excel形式でダウンロード",
                data=excel_bytes,
                file_name=f"シフト表_{st.session_state['last_result_month']}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
