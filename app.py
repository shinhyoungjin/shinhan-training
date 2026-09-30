import hmac
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime as dt

import gspread
import pandas as pd
import pytz
import streamlit as st
from google.oauth2.service_account import Credentials

# ==========================================================
# 상수
# ==========================================================
DB_PATH = "training_data.db"
DB_SAVE_INTERVAL = 5          # DB 자동저장(=하트비트) 주기(초)
SHEET_SYNC_INTERVAL = 60      # 구글 시트 동기화 주기(초)
HEARTBEAT_TIMEOUT = 20        # 이 시간 이상 하트비트가 없으면 '죽은 세션'으로 간주(초)

SHEET_HEADER = [
    "세션ID", "로그ID", "등록번호", "성명", "이메일",
    "시청시작시각(KST)", "최종시청/저장시각(KST)",
    "해당세션_시청시간", "해당세션_시청초", "상태",
]

st.set_page_config(
    page_title="법인 임직원 온라인 교육 시스템",
    page_icon="🎓",
    layout="wide",
)


# ==========================================================
# 공통 유틸
# ==========================================================
def get_kst_now_str():
    kst = pytz.timezone("Asia/Seoul")
    return dt.now(kst).strftime("%Y-%m-%d %H:%M:%S")


def fmt_sec(sec):
    sec = int(sec)
    return f"{sec // 60}분 {sec % 60}초"


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ==========================================================
# DB 초기화 / 설정
# ==========================================================
def init_db():
    with db() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                id INTEGER PRIMARY KEY,
                video_url TEXT,
                target_minutes INTEGER
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS watch_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_uuid TEXT,
                registration_number TEXT,
                name TEXT,
                email TEXT,
                session_start_time TEXT,
                session_end_time TEXT,
                session_seconds INTEGER,
                last_heartbeat REAL DEFAULT 0,
                is_active INTEGER DEFAULT 0
            )
        """)
        # 구버전 DB가 남아 있는 경우를 위한 컬럼 보강
        cols = {r[1] for r in conn.execute("PRAGMA table_info(watch_logs)")}
        if "session_uuid" not in cols:
            conn.execute("ALTER TABLE watch_logs ADD COLUMN session_uuid TEXT")
        if "last_heartbeat" not in cols:
            conn.execute("ALTER TABLE watch_logs ADD COLUMN last_heartbeat REAL DEFAULT 0")
        if "is_active" not in cols:
            conn.execute("ALTER TABLE watch_logs ADD COLUMN is_active INTEGER DEFAULT 0")

        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_watch_logs_uuid ON watch_logs(session_uuid)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_watch_logs_reg ON watch_logs(registration_number)"
        )

        if conn.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 0:
            default_url = st.secrets.get(
                "default_video_url", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
            )
            default_min = int(st.secrets.get("default_target_minutes", 50))
            conn.execute(
                "INSERT INTO settings (id, video_url, target_minutes) VALUES (1, ?, ?)",
                (default_url, default_min),
            )


def get_settings():
    with db() as conn:
        row = conn.execute(
            "SELECT video_url, target_minutes FROM settings WHERE id = 1"
        ).fetchone()
    return {"url": row[0], "target_min": row[1]}


# ==========================================================
# 구글 시트 (백업 + 복원)
# ==========================================================
def get_gspread_client():
    if "gcp_service_account" not in st.secrets:
        raise RuntimeError("secrets에 gcp_service_account가 없습니다.")
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(
        st.secrets["gcp_service_account"], scopes=scopes
    )
    return gspread.authorize(creds)


@st.cache_resource
def get_sheet():
    """시트 객체를 프로세스 단위로 캐시하고, 헤더는 최초 1회만 확인합니다."""
    if "backup_sheet_url" not in st.secrets:
        raise RuntimeError("secrets에 backup_sheet_url이 없습니다.")
    client = get_gspread_client()
    sheet = client.open_by_url(st.secrets["backup_sheet_url"]).sheet1
    if not sheet.row_values(1):
        sheet.update(values=[SHEET_HEADER], range_name="A1:J1")
    return sheet


def sync_to_sheet(session_uuid, is_final=False):
    """DB에 저장된 세션 값을 읽어 시트에 반영합니다. (세션ID로 행을 찾아 덮어쓰기)"""
    try:
        with db() as conn:
            row = conn.execute(
                """
                SELECT id, registration_number, name, email,
                       session_start_time, session_end_time, session_seconds
                FROM watch_logs WHERE session_uuid = ?
                """,
                (session_uuid,),
            ).fetchone()
        if not row:
            return False

        log_id, reg, name, email, start, end, sec = row
        status = "시청 완료 (정지)" if is_final else "시청 중 (자동저장)"
        values = [session_uuid, log_id, reg, name, email, start, end,
                  fmt_sec(sec), sec, status]

        sheet = get_sheet()
        cell = sheet.find(session_uuid, in_column=1)
        if cell:
            sheet.update(values=[values], range_name=f"A{cell.row}:J{cell.row}", raw=True)
        else:
            sheet.append_row(values, value_input_option="RAW")

        st.session_state.last_sync_ok = True
        return True
    except Exception as e:
        st.session_state.last_sync_ok = False
        print(f"구글 시트 동기화 실패: {e}")
        get_sheet.clear()  # 다음 시도에서 재연결
        return False


@st.cache_resource
def restore_from_sheet_if_empty():
    """
    서버(프로세스) 시작 시 1회 실행.
    DB에 기록이 하나도 없으면 구글 시트의 기록으로 복원합니다.
    실패하면 예외를 던지므로 캐시되지 않고 다음 실행 때 재시도됩니다.
    """
    with db() as conn:
        if conn.execute("SELECT COUNT(*) FROM watch_logs").fetchone()[0] > 0:
            return 0

    rows = get_sheet().get_all_values()[1:]  # 헤더 제외
    restored = 0
    with db() as conn:
        for r in rows:
            if len(r) < 9 or not r[0]:
                continue
            try:
                sec = int(r[8])
            except ValueError:
                continue
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO watch_logs
                (session_uuid, registration_number, name, email,
                 session_start_time, session_end_time, session_seconds,
                 last_heartbeat, is_active)
                VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0)
                """,
                (r[0], r[2], r[3], r[4], r[5], r[6], sec),
            )
            restored += cur.rowcount
    return restored


# ==========================================================
# 시청 세션 DB 함수
# ==========================================================
def try_start_session(session_uuid, reg_num, name, email, start_str):
    """
    같은 등록번호로 활성 세션이 없을 때만 새 세션을 생성합니다.
    BEGIN IMMEDIATE로 '확인 + 등록'을 원자적으로 처리하여 동시 클릭도 막습니다.
    """
    conn = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        cutoff = time.time() - HEARTBEAT_TIMEOUT
        active = conn.execute(
            """
            SELECT COUNT(*) FROM watch_logs
            WHERE registration_number = ? AND is_active = 1 AND last_heartbeat > ?
            """,
            (reg_num, cutoff),
        ).fetchone()[0]
        if active > 0:
            conn.execute("ROLLBACK")
            return False
        conn.execute(
            """
            INSERT INTO watch_logs
            (session_uuid, registration_number, name, email,
             session_start_time, session_end_time, session_seconds,
             last_heartbeat, is_active)
            VALUES (?, ?, ?, ?, ?, ?, 0, ?, 1)
            """,
            (session_uuid, reg_num, name, email, start_str, start_str, time.time()),
        )
        conn.execute("COMMIT")
        return True
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()


def has_other_active_session(reg_num, my_uuid):
    cutoff = time.time() - HEARTBEAT_TIMEOUT
    with db() as conn:
        n = conn.execute(
            """
            SELECT COUNT(*) FROM watch_logs
            WHERE registration_number = ? AND is_active = 1 AND last_heartbeat > ?
              AND (session_uuid IS NULL OR session_uuid <> ?)
            """,
            (reg_num, cutoff, my_uuid),
        ).fetchone()[0]
    return n > 0


def upsert_watch_session(session_uuid, reg_num, name, email, start, end, sec, active):
    with db() as conn:
        conn.execute(
            """
            INSERT INTO watch_logs
            (session_uuid, registration_number, name, email,
             session_start_time, session_end_time, session_seconds,
             last_heartbeat, is_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_uuid) DO UPDATE SET
              session_end_time = excluded.session_end_time,
              session_seconds  = excluded.session_seconds,
              last_heartbeat   = excluded.last_heartbeat,
              is_active        = excluded.is_active
            """,
            (session_uuid, reg_num, name, email, start, end, sec,
             time.time(), 1 if active else 0),
        )


def delete_session(session_uuid):
    with db() as conn:
        conn.execute("DELETE FROM watch_logs WHERE session_uuid = ?", (session_uuid,))


def get_user_total_seconds(reg_num, exclude_uuid=None):
    with db() as conn:
        r = conn.execute(
            """
            SELECT COALESCE(SUM(session_seconds), 0) FROM watch_logs
            WHERE registration_number = ? AND (session_uuid IS NULL OR session_uuid <> ?)
            """,
            (reg_num, exclude_uuid or ""),
        ).fetchone()
    return int(r[0])


def get_user_logs(reg_num):
    with db() as conn:
        return pd.read_sql(
            """
            SELECT session_start_time, session_end_time, session_seconds
            FROM watch_logs
            WHERE registration_number = ? AND session_seconds > 0
            ORDER BY id DESC
            """,
            conn,
            params=(reg_num,),
        )


# ==========================================================
# 세션 상태 관리
# ==========================================================
def reset_play_state():
    st.session_state.is_playing = False
    st.session_state.current_session_sec = 0
    st.session_state.session_start_ts = None
    st.session_state.session_start_str = None
    st.session_state.session_uuid = None
    st.session_state.session_user = None
    st.session_state.last_db_save = 0
    st.session_state.last_sheet_sync = 0


def finalize_session():
    """진행 중인 세션을 DB에 최종 저장(비활성화)하고 시트에 마지막으로 반영합니다."""
    ss = st.session_state
    if ss.is_playing and ss.session_uuid and ss.session_user:
        sec = int(time.time() - ss.session_start_ts)
        if sec > 0:
            u = ss.session_user
            upsert_watch_session(
                ss.session_uuid, u["reg"], u["name"], u["email"],
                ss.session_start_str, get_kst_now_str(), sec, active=False,
            )
            sync_to_sheet(ss.session_uuid, is_final=True)
        else:
            delete_session(ss.session_uuid)
    reset_play_state()


for key, default in {
    "selected_reg_num": None,
    "last_sync_ok": None,
    "notice": None,
}.items():
    if key not in st.session_state:
        st.session_state[key] = default
if "is_playing" not in st.session_state:
    reset_play_state()


# ==========================================================
# 명단 (구글 시트 CSV)
# ==========================================================
@st.cache_data(ttl=600, show_spinner=False)
def get_users_from_google_sheet():
    sheet_url = (
        "https://docs.google.com/spreadsheets/d/"
        "1kC87Ec4T2S0gGu28vI_Hzt5THhXuvZPK1P88hfeFEYI/export?format=csv&gid=0"
    )
    df = pd.read_csv(sheet_url, dtype=str).fillna("")  # 예외는 밖으로 → 실패 결과는 캐시되지 않음
    df = df.rename(columns={
        "등록번호": "registration_number",
        "성명": "name",
        "rsm 메일": "email",
    })
    df["registration_number"] = df["registration_number"].str.strip()
    return df[["registration_number", "name", "email"]]


# ==========================================================
# 초기화 실행
# ==========================================================
init_db()
try:
    restored_n = restore_from_sheet_if_empty()
    if restored_n:
        st.toast(f"구글 시트에서 {restored_n}건의 시청 기록을 복원했습니다.", icon="♻️")
except Exception as e:
    st.warning(f"⚠ 구글 시트 복원 확인 중 오류가 발생했습니다(다음 접속 시 재시도): {e}")

settings = get_settings()
target_seconds = settings["target_min"] * 60


# ==========================================================
# 메인 UI - 사이드바
# ==========================================================
st.title("🎓 법인 임직원 법정의무/자체 온라인 교육")
st.caption("시청 완료 조건: 지정된 누적 시간 이상 시청 시 자동 이수 완료")

st.sidebar.header("👤 수강자 확인")

current_user = None
try:
    users_df = get_users_from_google_sheet()
except Exception as e:
    users_df = pd.DataFrame(columns=["registration_number", "name", "email"])
    st.sidebar.error(f"명단을 불러오지 못했습니다: {e}")

valid_users = users_df[users_df["name"] != ""].reset_index(drop=True)

if not valid_users.empty:
    idx = st.sidebar.selectbox(
        "본인의 이름을 검색하여 선택하세요 (오탈자 방지)",
        options=[-1] + list(range(len(valid_users))),
        format_func=lambda i: "선택하세요" if i == -1 else (
            f"{valid_users.loc[i, 'name']} "
            f"(등록번호: {valid_users.loc[i, 'registration_number']}) "
            f"- {valid_users.loc[i, 'email']}"
        ),
    )

    if idx != -1:
        current_user = valid_users.loc[idx]
        reg_num = current_user["registration_number"]

        if st.session_state.selected_reg_num != reg_num:
            # 수강자를 바꾸기 전에 진행 중이던 세션을 안전하게 저장/종료
            finalize_session()
            st.session_state.selected_reg_num = reg_num

        st.sidebar.success(f"확인됨: **{current_user['name']}** 님")
    else:
        if st.session_state.selected_reg_num is not None:
            finalize_session()
            st.session_state.selected_reg_num = None

st.sidebar.markdown("---")

# ---------------- 관리자 메뉴 ----------------
with st.sidebar.expander("⚙ 관리자 메뉴"):
    admin_secret = st.secrets.get("admin_password", "")
    admin_pw = st.text_input("관리자 비밀번호", type="password")

    if not admin_secret:
        st.warning("secrets에 admin_password가 설정되지 않아 관리자 메뉴가 비활성화되어 있습니다.")
    elif admin_pw and hmac.compare_digest(admin_pw.encode(), str(admin_secret).encode()):
        st.success("관리자 인증 성공")

        st.subheader("1. 교육 영상 및 시간 교체")
        new_url = st.text_input("유튜브 영상 URL", value=settings["url"])
        new_target = st.number_input("목표 시청시간(분)", value=settings["target_min"], min_value=1)
        if st.button("설정 저장"):
            with db() as conn:
                conn.execute(
                    "UPDATE settings SET video_url = ?, target_minutes = ? WHERE id = 1",
                    (new_url, int(new_target)),
                )
            st.success("저장되었습니다!")
            st.rerun()
        st.caption("※ 서버 재시작 시 설정은 secrets의 기본값으로 돌아갑니다.")

        st.subheader("2. 시청 기록 다운로드")
        with db() as conn:
            logs_df = pd.read_sql(
                "SELECT id, registration_number, name, email, session_start_time, "
                "session_end_time, session_seconds FROM watch_logs WHERE session_seconds > 0",
                conn,
            )

        if not logs_df.empty:
            summary_df = logs_df.groupby(["registration_number", "name", "email"]).agg(
                총_시청_초=("session_seconds", "sum"),
                시청_횟수=("id", "count"),
                최초_시청일시=("session_start_time", "min"),
                최종_시청일시=("session_end_time", "max"),
            ).reset_index()

            summary_df["총_시청_시간"] = summary_df["총_시청_초"].apply(fmt_sec)
            summary_df["이수_완료_여부"] = summary_df["총_시청_초"].apply(
                lambda x: "완료" if x >= target_seconds else "미완료(진행중)"
            )
            summary_export = summary_df.rename(columns={
                "registration_number": "등록번호", "name": "성명", "email": "이메일",
            })[["등록번호", "성명", "이메일", "총_시청_시간", "이수_완료_여부",
                "시청_횟수", "최초_시청일시", "최종_시청일시"]]

            today = get_kst_now_str()[:10].replace("-", "")
            st.download_button(
                "📥 1. 인별 총 시청 집계표 (요약) 다운로드",
                data=summary_export.to_csv(index=False).encode("utf-8-sig"),
                file_name=f"교육이수_요약집계표_{today}.csv",
                mime="text/csv",
            )

            logs_export = logs_df.rename(columns={
                "id": "로그ID", "registration_number": "등록번호", "name": "성명",
                "email": "이메일", "session_start_time": "시청시작시각(KST)",
                "session_end_time": "최종시청/저장시각(KST)",
                "session_seconds": "해당세션_시청초",
            })
            logs_export["해당세션_시청시간"] = logs_export["해당세션_시청초"].apply(fmt_sec)
            logs_export = logs_export[[
                "로그ID", "등록번호", "성명", "이메일", "시청시작시각(KST)",
                "최종시청/저장시각(KST)", "해당세션_시청시간", "해당세션_시청초",
            ]]
            st.download_button(
                "📥 2. 개별 시청 상세 이력 로그 다운로드",
                data=logs_export.to_csv(index=False).encode("utf-8-sig"),
                file_name=f"개별_시청상세로그_{today}.csv",
                mime="text/csv",
            )
        else:
            st.info("아직 저장된 시청 기록이 없습니다.")
    elif admin_pw:
        st.error("비밀번호가 올바르지 않습니다.")


# ==========================================================
# 메인 영역 - 시청
# ==========================================================
if current_user is None:
    st.warning("👈 왼쪽 사이드바에서 본인의 이름을 먼저 선택해 주세요.")
else:
    ss = st.session_state
    reg = current_user["registration_number"]

    if ss.notice:
        st.error(ss.notice)
        ss.notice = None

    # 1. 실제 경과 시간 (System Clock 기반)
    if ss.is_playing and ss.session_start_ts:
        ss.current_session_sec = int(time.time() - ss.session_start_ts)

    # 2. 누적 시청시간 = (이번 세션 제외한 DB 합계) + 이번 세션 실시간
    total_watched_sec = get_user_total_seconds(reg, ss.session_uuid) + ss.current_session_sec

    st.subheader(f"📌 교육 영상 (목표 시청시간: {settings['target_min']}분)")
    st.video(settings["url"])

    col1, col2 = st.columns([1, 2])

    with col1:
        st.markdown("### ⏱️ 시청 시간 측정")

        if not ss.is_playing:
            if st.button("▶️ 영상 시청 시작 / 재개", use_container_width=True):
                new_uuid = uuid.uuid4().hex
                start_str = get_kst_now_str()
                if try_start_session(new_uuid, reg, current_user["name"],
                                     current_user["email"], start_str):
                    ss.is_playing = True
                    ss.session_uuid = new_uuid
                    ss.session_user = {
                        "reg": reg,
                        "name": current_user["name"],
                        "email": current_user["email"],
                    }
                    ss.session_start_ts = time.time()
                    ss.session_start_str = start_str
                    ss.current_session_sec = 0
                    ss.last_db_save = time.time()
                    ss.last_sheet_sync = time.time()
                    st.rerun()
                else:
                    st.error(
                        "🚫 이미 다른 창/기기에서 시청 중입니다. "
                        f"해당 창에서 '일시 정지'를 누르거나, 창을 닫은 뒤 약 {HEARTBEAT_TIMEOUT}초 후 다시 시도하세요."
                    )
        else:
            if st.button("⏸️ 일시 정지 및 저장", use_container_width=True):
                finalize_session()
                st.rerun()

        progress = min(total_watched_sec / target_seconds, 1.0)
        st.progress(progress)
        st.metric("총 누적 시청 시간", f"{fmt_sec(total_watched_sec)} / {settings['target_min']}분")
        st.caption("동일 계정으로는 한 번에 하나의 창에서만 시청 시간이 누적됩니다.")

    with col2:
        st.markdown("### 📝 이수 상태")
        if total_watched_sec >= target_seconds:
            st.success("🎉 필수 시청 시간을 모두 충족하여 이수가 완료되었습니다!")
        else:
            remaining = target_seconds - total_watched_sec
            st.info(f"목표 시간까지 **{fmt_sec(remaining)}** 남았습니다.")

        if ss.last_sync_ok is True:
            st.caption("☁️ 구글 시트 백업: 정상")
        elif ss.last_sync_ok is False:
            st.warning("⚠ 구글 시트 백업에 실패했습니다. 시청 기록은 서버 DB에 저장되어 있으며 다음 저장 시 재시도됩니다.")

    # 개인 시청 이력
    st.markdown("---")
    st.subheader(f"📊 [{current_user['name']} 님]의 개인 교육 이수 현황")

    user_logs_df = get_user_logs(reg)
    if not user_logs_df.empty:
        user_logs_df["시청 시간"] = user_logs_df["session_seconds"].apply(fmt_sec)
        user_logs_df = user_logs_df.rename(columns={
            "session_start_time": "시청 시작 시각 (KST)",
            "session_end_time": "시청 종료/저장 시각 (KST)",
        })[["시청 시작 시각 (KST)", "시청 종료/저장 시각 (KST)", "시청 시간"]]
        st.dataframe(user_logs_df, use_container_width=True)
    else:
        st.info("아직 저장된 시청 이력이 없습니다. 영상 시청을 시작하시면 기록이 생성됩니다.")

    # 실시간 루프: 5초마다 DB 저장(하트비트), 60초마다 시트 동기화
    if ss.is_playing:
        now = time.time()

        if now - ss.last_db_save >= DB_SAVE_INTERVAL:
            u = ss.session_user

            # 다른 창이 이미 이어받았다면 이 창의 시청은 중단
            if has_other_active_session(u["reg"], ss.session_uuid):
                finalize_session()
                ss.notice = "다른 창/기기에서 시청이 시작되어 이 창의 시청이 종료되었습니다."
                st.rerun()

            upsert_watch_session(
                ss.session_uuid, u["reg"], u["name"], u["email"],
                ss.session_start_str, get_kst_now_str(), ss.current_session_sec, active=True,
            )
            ss.last_db_save = now

        if now - ss.last_sheet_sync >= SHEET_SYNC_INTERVAL:
            sync_to_sheet(ss.session_uuid, is_final=False)
            ss.last_sheet_sync = now

        time.sleep(1)
        st.rerun()
