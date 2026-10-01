import hmac
import logging
import os
import sqlite3
import threading
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
# 진단 로그 (테스트용)
# ==========================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [DIAG] %(message)s",
)
logger = logging.getLogger("training_app_diag")
APP_START_TS = time.perf_counter()

def diag(msg):
    logger.info(msg)

def diag_elapsed(label, start_ts):
    elapsed = time.perf_counter() - start_ts
    logger.info("%s 완료: elapsed=%.3fs", label, elapsed)
    return elapsed

diag("===== 앱 요청/실행 시작 =====")
diag("process=%s", os.getpid())

# ==========================================================
# 상수
# ==========================================================
DB_PATH = "training_data.db"
NUM_COURSES = 3               # 과목(영상) 수
DB_SAVE_INTERVAL = 5          # DB 자동저장(=하트비트) 주기(초)
SHEET_SYNC_INTERVAL = 60      # 구글 시트 배치 동기화 주기(초)
MIN_BATCH_GAP = 10            # 배치 동기화 최소 간격(초) - 쿼터 보호
HEARTBEAT_TIMEOUT = 20        # 이 시간 이상 하트비트가 없으면 '죽은 세션'으로 간주(초)

SETTINGS_TAB = "과목설정"
SETTINGS_HEADER = ["과목ID", "과목명", "영상URL", "목표시청분", "사용여부"]
SHEET_HEADER = [
    "세션ID", "로그ID", "등록번호", "성명", "이메일", "과목ID", "과목명",
    "시청시작시각(KST)", "최종시청/저장시각(KST)",
    "해당세션_시청시간", "해당세션_시청초", "상태",
]
LAST_COL = "L"  # SHEET_HEADER의 마지막 열 (12열)

STATUS_RUNNING = "시청 중 (자동저장)"
STATUS_DONE = "시청 완료 (정지)"
STATUS_ABNORMAL = "비정상 종료 (마지막 자동저장 기준)"

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


def fmt_epoch_kst(ts):
    kst = pytz.timezone("Asia/Seoul")
    return dt.fromtimestamp(ts, kst).strftime("%H:%M:%S")


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
# DB 초기화
# ==========================================================
def init_db():
    _diag_ts = time.perf_counter()
    diag("init_db 시작")
    with db() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS courses (
                slot_id INTEGER PRIMARY KEY,
                title TEXT,
                video_url TEXT,
                target_minutes INTEGER,
                enabled INTEGER DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS watch_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_uuid TEXT,
                registration_number TEXT,
                name TEXT,
                email TEXT,
                course_id INTEGER,
                course_title TEXT,
                session_start_time TEXT,
                session_end_time TEXT,
                session_seconds INTEGER,
                last_heartbeat REAL DEFAULT 0,
                is_active INTEGER DEFAULT 0,
                synced_seconds INTEGER
            )
        """)
        # 구버전 DB 보강
        cols = {r[1] for r in conn.execute("PRAGMA table_info(watch_logs)")}
        for name, ddl in [
            ("session_uuid", "TEXT"),
            ("last_heartbeat", "REAL DEFAULT 0"),
            ("is_active", "INTEGER DEFAULT 0"),
            ("synced_seconds", "INTEGER"),
            ("course_title", "TEXT"),
        ]:
            if name not in cols:
                conn.execute(f"ALTER TABLE watch_logs ADD COLUMN {name} {ddl}")
        if "course_id" not in cols:
            conn.execute("ALTER TABLE watch_logs ADD COLUMN course_id INTEGER")
            conn.execute("UPDATE watch_logs SET course_id = 1 WHERE course_id IS NULL")

        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_watch_logs_uuid ON watch_logs(session_uuid)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_watch_logs_reg ON watch_logs(registration_number)"
        )


# ==========================================================
# 구글 시트 백업 (배치 동기화 + 복원)
# ==========================================================
class SheetBackup:
    """
    - 화면 세션은 시트를 직접 호출하지 않습니다.
    - 백그라운드 스레드 1개가 미반영 세션을 모아 한 번에 시트에 씁니다.
    - 이 클래스는 streamlit(st.*)에 의존하지 않으므로 스레드에서 안전하게 동작합니다.
    """

    def __init__(self, creds_info, sheet_url):
        self.creds_info = creds_info
        self.sheet_url = sheet_url
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.state = {"ok": None, "last_success": None, "error": None}
        self._spreadsheet = None
        self._header_ok = False
        self._last_batch = 0.0
        threading.Thread(target=self._loop, daemon=True, name="sheet-backup").start()

    # ---- 연결 ----
    @contextmanager
    def _guard(self):
        with self.lock:
            try:
                yield
            except Exception:
                self._spreadsheet = None   # 다음 시도에서 재연결
                self._header_ok = False
                raise

    def _open(self):
        if self._spreadsheet is None:
            scopes = [
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive",
            ]
            creds = Credentials.from_service_account_info(self.creds_info, scopes=scopes)
            client = gspread.authorize(creds)
            try:
                client.set_timeout(30)
            except Exception:
                pass
            self._spreadsheet = client.open_by_url(self.sheet_url)
        return self._spreadsheet

    def _log_sheet(self):
        ws = self._open().sheet1
        if not self._header_ok:
            first = ws.row_values(1)
            if not first:
                ws.update(values=[SHEET_HEADER], range_name=f"A1:{LAST_COL}1")
            elif first[:len(SHEET_HEADER)] != SHEET_HEADER:
                raise RuntimeError(
                    "백업 시트의 1행(헤더)이 현재 형식과 다릅니다. "
                    "새 구글 시트를 사용하거나 시트 내용을 모두 지운 뒤 다시 시도하세요."
                )
            self._header_ok = True
        return ws

    # ---- 시청 로그 ----
    def read_log_rows(self):
        with self._guard():
            return self._log_sheet().get_all_values()[1:]

    def run_batch(self):
        """DB에서 미반영 세션을 모두 읽어 시트에 일괄 반영합니다."""
        now = time.time()
        cutoff = now - HEARTBEAT_TIMEOUT
        with db() as conn:
            rows = conn.execute(
                """
                SELECT session_uuid, id, registration_number, name, email,
                       course_id, course_title, session_start_time, session_end_time,
                       session_seconds, is_active, last_heartbeat
                FROM watch_logs
                WHERE session_uuid IS NOT NULL AND session_seconds > 0
                  AND (synced_seconds IS NULL OR synced_seconds <> session_seconds)
                """
            ).fetchall()

        if not rows:
            self.state.update(ok=True, error=None)
            return 0

        with self._guard():
            ws = self._log_sheet()
            pos = {}
            for i, v in enumerate(ws.col_values(1), start=1):
                if v and v not in pos:
                    pos[v] = i

            updates, appends, done, stale = [], [], [], []
            for (uid, log_id, reg, name, email, cid, ctitle, start, end,
                 sec, active, hb) in rows:
                if active and hb < cutoff:
                    status = STATUS_ABNORMAL
                    stale.append(uid)
                elif active:
                    status = STATUS_RUNNING
                else:
                    status = STATUS_DONE
                vals = [uid, log_id, reg, name, email, cid, ctitle or "",
                        start, end, fmt_sec(sec), sec, status]
                if uid in pos:
                    r = pos[uid]
                    updates.append({"range": f"A{r}:{LAST_COL}{r}", "values": [vals]})
                else:
                    appends.append(vals)
                done.append((sec, uid))

            if updates:
                ws.batch_update(updates, value_input_option="RAW")
            if appends:
                ws.append_rows(appends, value_input_option="RAW")

        with db() as conn:
            conn.executemany(
                "UPDATE watch_logs SET synced_seconds = ? WHERE session_uuid = ?", done
            )
            if stale:
                conn.executemany(
                    "UPDATE watch_logs SET is_active = 0 "
                    "WHERE session_uuid = ? AND last_heartbeat < ?",
                    [(u, cutoff) for u in stale],
                )
        self.state.update(ok=True, error=None, last_success=time.time())
        return len(rows)

    def _loop(self):
        while True:
            self.wake.wait(timeout=SHEET_SYNC_INTERVAL)
            self.wake.clear()
            gap = time.time() - self._last_batch
            if gap < MIN_BATCH_GAP:
                time.sleep(MIN_BATCH_GAP - gap)
            try:
                self.run_batch()
            except Exception as e:
                self.state.update(ok=False, error=str(e))
                print(f"구글 시트 배치 동기화 실패: {e}")
            self._last_batch = time.time()

    # ---- 과목 설정 ----
    def save_courses(self, rows):
        """rows: [(slot_id, title, url, target_min, enabled), ...]"""
        with self._guard():
            ss = self._open()
            try:
                ws = ss.worksheet(SETTINGS_TAB)
            except gspread.WorksheetNotFound:
                ws = ss.add_worksheet(title=SETTINGS_TAB, rows=10, cols=6)
            ws.clear()
            ws.update(values=[SETTINGS_HEADER] + [list(r) for r in rows], range_name="A1")

    def load_courses(self):
        """저장된 과목 설정을 [(slot_id, title, url, target_min, enabled), ...]로 반환. 없으면 []"""
        with self._guard():
            ss = self._open()
            try:
                ws = ss.worksheet(SETTINGS_TAB)
            except gspread.WorksheetNotFound:
                return []
            values = ws.get_all_values()[1:]
        found = {}
        for r in values:
            if len(r) < 5:
                continue
            try:
                slot = int(r[0])
                target = int(float(r[3]))
                enabled = 1 if str(r[4]).strip() in ("1", "True", "TRUE", "true") else 0
            except ValueError:
                continue
            if 1 <= slot <= NUM_COURSES:
                found[slot] = (slot, r[1], r[2], max(target, 1), enabled)
        if not found:
            return []
        for slot in range(1, NUM_COURSES + 1):
            found.setdefault(slot, (slot, f"과목 {slot}", "", 50, 0))
        return [found[s] for s in sorted(found)]


    diag_elapsed("init_db", _diag_ts)

@st.cache_resource
def get_backup():
    if "gcp_service_account" not in st.secrets or "backup_sheet_url" not in st.secrets:
        return None
    return SheetBackup(dict(st.secrets["gcp_service_account"]), st.secrets["backup_sheet_url"])


def wake_backup():
    b = st.session_state.get("_backup_ref")
    if b is not None:
        b.wake.set()


@st.cache_resource
def restore_logs_once(_backup):
    """
    서버(프로세스) 시작 후 1회: DB에 시청 기록이 없으면 시트에서 복원합니다.
    실패하면 예외 → 캐시되지 않아 다음 실행 때 재시도됩니다.
    """
    with db() as conn:
        if conn.execute("SELECT COUNT(*) FROM watch_logs").fetchone()[0] > 0:
            return 0

    rows = _backup.read_log_rows()
    restored = 0
    with db() as conn:
        for r in rows:
            if len(r) < 11 or not r[0]:
                continue
            try:
                sec = int(r[10])
            except ValueError:
                continue
            try:
                cid = int(r[5])
            except ValueError:
                cid = None
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO watch_logs
                (session_uuid, registration_number, name, email, course_id, course_title,
                 session_start_time, session_end_time, session_seconds,
                 last_heartbeat, is_active, synced_seconds)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?)
                """,
                (r[0], r[2], r[3], r[4], cid, r[6], r[7], r[8], sec, sec),
            )
            restored += cur.rowcount
    return restored


# ==========================================================
# 과목 설정
# ==========================================================
def ensure_courses(backup):
    """courses 테이블이 비어 있으면 시트에서 복원하고, 없으면 기본값으로 채웁니다."""
    with db() as conn:
        if conn.execute("SELECT COUNT(*) FROM courses").fetchone()[0] > 0:
            return True

    rows = []
    if backup is not None:
        try:
            rows = backup.load_courses()
        except Exception as e:
            st.error(f"과목 설정을 구글 시트에서 불러오지 못했습니다. 잠시 후 새로고침 해주세요: {e}")
            return False

    if not rows:
        default_url = st.secrets.get(
            "default_video_url", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        )
        default_min = int(st.secrets.get("default_target_minutes", 50))
        rows = [(1, "과목 1", default_url, default_min, 1)] + [
            (i, f"과목 {i}", "", 50, 0) for i in range(2, NUM_COURSES + 1)
        ]

    with db() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO courses "
            "(slot_id, title, video_url, target_minutes, enabled) VALUES (?, ?, ?, ?, ?)",
            rows,
        )
    return True


def get_courses():
    with db() as conn:
        rows = conn.execute(
            "SELECT slot_id, title, video_url, target_minutes, enabled "
            "FROM courses ORDER BY slot_id"
        ).fetchall()
    return [
        {"id": r[0], "title": r[1] or f"과목 {r[0]}", "url": r[2] or "",
         "target_min": int(r[3] or 50), "enabled": bool(r[4])}
        for r in rows
    ]


# ==========================================================
# 시청 세션 DB 함수
# ==========================================================
def try_start_session(session_uuid, reg_num, name, email, course, start_str):
    """같은 등록번호의 활성 세션이 없을 때만 새 세션을 생성합니다. (확인+등록 원자적 처리)"""
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
            (session_uuid, registration_number, name, email, course_id, course_title,
             session_start_time, session_end_time, session_seconds,
             last_heartbeat, is_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 1)
            """,
            (session_uuid, reg_num, name, email, course["id"], course["title"],
             start_str, start_str, time.time()),
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


def upsert_watch_session(session_uuid, user, course, start, end, sec, active):
    with db() as conn:
        conn.execute(
            """
            INSERT INTO watch_logs
            (session_uuid, registration_number, name, email, course_id, course_title,
             session_start_time, session_end_time, session_seconds,
             last_heartbeat, is_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_uuid) DO UPDATE SET
              session_end_time = excluded.session_end_time,
              session_seconds  = excluded.session_seconds,
              last_heartbeat   = excluded.last_heartbeat,
              is_active        = excluded.is_active
            """,
            (session_uuid, user["reg"], user["name"], user["email"],
             course["id"], course["title"], start, end, sec,
             time.time(), 1 if active else 0),
        )


def delete_session(session_uuid):
    with db() as conn:
        conn.execute("DELETE FROM watch_logs WHERE session_uuid = ?", (session_uuid,))


def get_course_stats(reg_num, exclude_uuid=None):
    """과목별 누적 시청초와 마지막 시청 시각 {course_id: {"sec":, "last":}}"""
    with db() as conn:
        rows = conn.execute(
            """
            SELECT course_id, COALESCE(SUM(session_seconds), 0), MAX(session_end_time)
            FROM watch_logs
            WHERE registration_number = ? AND session_seconds > 0
              AND (session_uuid IS NULL OR session_uuid <> ?)
            GROUP BY course_id
            """,
            (reg_num, exclude_uuid or ""),
        ).fetchall()
    return {r[0]: {"sec": int(r[1]), "last": r[2]} for r in rows}


def get_user_logs(reg_num):
    with db() as conn:
        return pd.read_sql(
            """
            SELECT course_id, course_title, session_start_time, session_end_time, session_seconds
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
    ss = st.session_state
    ss.is_playing = False
    ss.current_session_sec = 0
    ss.session_start_ts = None
    ss.session_start_str = None
    ss.session_uuid = None
    ss.session_user = None
    ss.session_course = None
    ss.base_stats = {}
    ss.last_db_save = 0


def finalize_session():
    """진행 중인 세션을 DB에 최종 저장(비활성화)하고 백업 스레드를 깨웁니다."""
    ss = st.session_state
    if ss.is_playing and ss.session_uuid and ss.session_user and ss.session_course:
        sec = int(time.time() - ss.session_start_ts)
        if sec > 0:
            upsert_watch_session(
                ss.session_uuid, ss.session_user, ss.session_course,
                ss.session_start_str, get_kst_now_str(), sec, active=False,
            )
            wake_backup()
        else:
            delete_session(ss.session_uuid)
    reset_play_state()


def heartbeat_tick():
    """(fragment 안에서 매초 호출) 경과 시간 갱신, 5초마다 DB 저장, 중복 세션 감지"""
    ss = st.session_state
    now = time.time()
    ss.current_session_sec = int(now - ss.session_start_ts)

    if now - ss.last_db_save >= DB_SAVE_INTERVAL:
        user = ss.session_user
        if has_other_active_session(user["reg"], ss.session_uuid):
            finalize_session()
            ss.notice = "다른 창/기기에서 시청이 시작되어 이 창의 시청이 종료되었습니다."
            st.rerun()
        upsert_watch_session(
            ss.session_uuid, user, ss.session_course, ss.session_start_str,
            get_kst_now_str(), ss.current_session_sec, active=True,
        )
        ss.last_db_save = now


# ==========================================================
# 명단 (구글 시트 CSV)
# ==========================================================
@st.cache_data(ttl=600, show_spinner=False)
def get_users_from_google_sheet():
    _diag_ts = time.perf_counter()
    diag("Google Sheets 수강자 명단 조회 시작")
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
# 대시보드 표
# ==========================================================
def build_dashboard_df(courses, stats, live_cid=None, live_sec=0):
    rows = []
    for c in courses:
        base = stats.get(c["id"], {})
        sec = base.get("sec", 0) + (live_sec if c["id"] == live_cid else 0)
        target = c["target_min"] * 60
        if sec >= target:
            status = "✅ 이수 완료"
        elif sec > 0:
            status = "⏳ 진행 중"
        else:
            status = "⚪ 미시작"
        last = "시청 중" if c["id"] == live_cid else (base.get("last") or "-")
        rows.append({
            "과목": c["title"],
            "상태": status,
            "누적 시청": fmt_sec(sec),
            "목표": f"{c['target_min']}분",
            "진행률": round(min(sec / target, 1.0) * 100),
            "마지막 시청": last,
        })
    return pd.DataFrame(rows)


def render_backup_status(backup):
    if backup is None:
        st.caption("☁️ 구글 시트 백업이 설정되어 있지 않습니다.")
        return
    s = backup.state
    if s["ok"] is False:
        st.warning("⚠ 구글 시트 백업이 지연되고 있습니다. 시청 기록은 서버에 저장되어 있으며 자동으로 재시도됩니다.")
    elif s["ok"] is True and s["last_success"]:
        st.caption(f"☁️ 구글 시트 백업: 정상 (마지막 반영 {fmt_epoch_kst(s['last_success'])})")
    else:
        st.caption("☁️ 구글 시트 백업: 다음 동기화 대기 중 (최대 1분)")


# ==========================================================
# ===== MAIN =====
# ==========================================================
init_db()
diag_elapsed("초기 import/환경 준비", APP_START_TS)
backup = get_backup()
st.session_state["_backup_ref"] = backup

if backup is None:
    st.warning("⚠ 구글 시트 백업이 설정되지 않았습니다(secrets의 gcp_service_account, backup_sheet_url 확인). "
               "기록이 서버 DB에만 저장됩니다.")

if not ensure_courses(backup):
    st.stop()

if backup is not None:
    try:
        n_restored = restore_logs_once(backup)
        if n_restored:
            st.toast(f"구글 시트에서 {n_restored}건의 시청 기록을 복원했습니다.", icon="♻️")
    except Exception as e:
        st.warning(f"⚠ 구글 시트 복원 확인 중 오류가 발생했습니다(다음 접속 시 재시도): {e}")

for key, default in {"selected_reg_num": None, "notice": None}.items():
    if key not in st.session_state:
        st.session_state[key] = default
if "is_playing" not in st.session_state:
    reset_play_state()

ss = st.session_state
courses = get_courses()
courses_by_id = {c["id"]: c for c in courses}

st.title("🎓 법인 임직원 법정의무/자체 온라인 교육")
st.caption("과목을 선택해 영상을 시청하세요. 과목별 목표 시청시간을 채우면 자동으로 이수 처리됩니다.")

# ---------------- 사이드바: 수강자 ----------------
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
        if ss.selected_reg_num != reg_num:
            finalize_session()  # 수강자를 바꾸기 전에 진행 중이던 세션을 안전하게 저장
            ss.selected_reg_num = reg_num
        st.sidebar.success(f"확인됨: **{current_user['name']}** 님")
    elif ss.selected_reg_num is not None:
        finalize_session()
        ss.selected_reg_num = None

st.sidebar.markdown("---")

# ---------------- 사이드바: 관리자 ----------------
with st.sidebar.expander("⚙ 관리자 메뉴"):
    admin_secret = st.secrets.get("admin_password", "")
    admin_pw = st.text_input("관리자 비밀번호", type="password")

    if not admin_secret:
        st.warning("secrets에 admin_password가 설정되지 않아 관리자 메뉴가 비활성화되어 있습니다.")
    elif admin_pw and hmac.compare_digest(admin_pw.encode(), str(admin_secret).encode()):
        st.success("관리자 인증 성공")

        st.subheader("1. 과목(영상) 설정")
        with st.form("course_form"):
            new_rows = []
            for c in courses:
                st.markdown(f"**과목 {c['id']}**")
                en = st.checkbox("사용", value=c["enabled"], key=f"en_{c['id']}")
                title = st.text_input("과목명", value=c["title"], key=f"ti_{c['id']}")
                url = st.text_input("유튜브 영상 URL", value=c["url"], key=f"ur_{c['id']}")
                tgt = st.number_input("목표 시청시간(분)", value=c["target_min"],
                                      min_value=1, key=f"tg_{c['id']}")
                new_rows.append((c["id"], title.strip() or f"과목 {c['id']}",
                                 url.strip(), int(tgt), 1 if en else 0))
                st.markdown("---")
            submitted = st.form_submit_button("과목 설정 저장")

        if submitted:
            with db() as conn:
                conn.executemany(
                    "INSERT OR REPLACE INTO courses "
                    "(slot_id, title, video_url, target_minutes, enabled) VALUES (?, ?, ?, ?, ?)",
                    new_rows,
                )
            if backup is not None:
                try:
                    backup.save_courses(new_rows)
                    st.success("저장되었고 구글 시트('과목설정' 탭)에도 백업되었습니다.")
                except Exception as e:
                    st.warning(f"서버에는 저장되었으나 시트 백업에 실패했습니다: {e}")
            else:
                st.success("저장되었습니다.")
            st.rerun()
        st.caption("※ 교육 시작 후 목표 시간을 바꾸면 기존 수강자의 이수 여부가 다시 계산됩니다.")

        if backup is not None and backup.state.get("error"):
            st.error(f"시트 백업 오류: {backup.state['error']}")

        st.subheader("2. 시청 기록 다운로드")
        with db() as conn:
            logs_df = pd.read_sql(
                "SELECT id, registration_number, name, email, course_id, course_title, "
                "session_start_time, session_end_time, session_seconds "
                "FROM watch_logs WHERE session_seconds > 0",
                conn,
            )

        if not logs_df.empty:
            logs_df["course_id"] = logs_df["course_id"].fillna(0).astype(int)
            title_map = {c["id"]: c["title"] for c in courses}
            target_map = {c["id"]: c["target_min"] for c in courses}

            summary = logs_df.groupby(
                ["registration_number", "name", "email", "course_id"]
            ).agg(
                총_시청_초=("session_seconds", "sum"),
                시청_횟수=("id", "count"),
                최초_시청일시=("session_start_time", "min"),
                최종_시청일시=("session_end_time", "max"),
            ).reset_index()
            summary["과목명"] = summary["course_id"].map(lambda i: title_map.get(i, "(미상)"))
            summary["목표_분"] = summary["course_id"].map(lambda i: target_map.get(i, 0))
            summary["총_시청_시간"] = summary["총_시청_초"].apply(fmt_sec)
            summary["이수_완료_여부"] = summary.apply(
                lambda r: "완료" if r["목표_분"] > 0 and r["총_시청_초"] >= r["목표_분"] * 60
                else "미완료(진행중)", axis=1)

            summary_export = summary.rename(columns={
                "registration_number": "등록번호", "name": "성명", "email": "이메일",
                "course_id": "과목ID",
            })[["등록번호", "성명", "이메일", "과목ID", "과목명", "목표_분", "총_시청_시간",
                "이수_완료_여부", "시청_횟수", "최초_시청일시", "최종_시청일시"]]

            today = get_kst_now_str()[:10].replace("-", "")
            st.download_button(
                "📥 1. 인별·과목별 집계표 다운로드",
                data=summary_export.to_csv(index=False).encode("utf-8-sig"),
                file_name=f"교육이수_과목별집계표_{today}.csv",
                mime="text/csv",
            )

            logs_export = logs_df.rename(columns={
                "id": "로그ID", "registration_number": "등록번호", "name": "성명",
                "email": "이메일", "course_id": "과목ID", "course_title": "과목명",
                "session_start_time": "시청시작시각(KST)",
                "session_end_time": "최종시청/저장시각(KST)",
                "session_seconds": "해당세션_시청초",
            })
            logs_export["해당세션_시청시간"] = logs_export["해당세션_시청초"].apply(fmt_sec)
            logs_export = logs_export[[
                "로그ID", "등록번호", "성명", "이메일", "과목ID", "과목명",
                "시청시작시각(KST)", "최종시청/저장시각(KST)",
                "해당세션_시청시간", "해당세션_시청초",
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
# 메인 영역
# ==========================================================
enabled_courses = [c for c in courses if c["enabled"] and c["url"]]

if current_user is None:
    st.warning("👈 왼쪽 사이드바에서 본인의 이름을 먼저 선택해 주세요.")
elif not enabled_courses:
    st.warning("등록된 교육 과목이 없습니다. 관리자에게 문의해 주세요.")
else:
    reg = current_user["registration_number"]

    if ss.notice:
        st.error(ss.notice)
        ss.notice = None

    # ---- 과목 선택 ----
    enabled_ids = [c["id"] for c in enabled_courses]
    if ss.is_playing and ss.session_course and ss.session_course["id"] in enabled_ids:
        ss["selected_course"] = ss.session_course["id"]
    if ss.get("selected_course") not in enabled_ids:
        ss["selected_course"] = enabled_ids[0]

    selected_id = st.radio(
        "📚 시청할 과목을 선택하세요 (시청 중에는 변경할 수 없습니다)",
        options=enabled_ids,
        format_func=lambda i: f"{courses_by_id[i]['title']} ({courses_by_id[i]['target_min']}분)",
        horizontal=True,
        key="selected_course",
        disabled=ss.is_playing,
    )

    shown = ss.session_course if (ss.is_playing and ss.session_course) else courses_by_id[selected_id]
    st.subheader(f"📌 {shown['title']} (목표 시청시간: {shown['target_min']}분)")
    st.video(shown["url"])

    # ---- 시작/정지 버튼 (클릭 시 전체 재실행) ----
    if not ss.is_playing:
        if st.button("▶️ 영상 시청 시작 / 재개"):
            course = courses_by_id[selected_id]
            new_uuid = uuid.uuid4().hex
            start_str = get_kst_now_str()
            if try_start_session(new_uuid, reg, current_user["name"],
                                 current_user["email"], course, start_str):
                ss.is_playing = True
                ss.session_uuid = new_uuid
                ss.session_user = {"reg": reg, "name": current_user["name"],
                                   "email": current_user["email"]}
                ss.session_course = dict(course)
                ss.base_stats = get_course_stats(reg, new_uuid)
                ss.session_start_ts = time.time()
                ss.session_start_str = start_str
                ss.current_session_sec = 0
                ss.last_db_save = time.time()
                st.rerun()
            else:
                st.error(
                    "🚫 이미 다른 창/기기에서 시청 중입니다. "
                    f"해당 창에서 '일시 정지'를 누르거나, 창을 닫은 뒤 약 {HEARTBEAT_TIMEOUT}초 후 다시 시도하세요."
                )
    else:
        if st.button("⏸️ 일시 정지 및 저장"):
            finalize_session()
            st.rerun()

    # ---- 타이머 + 대시보드 (이 부분만 매초 갱신) ----
    @st.fragment(run_every=1 if ss.is_playing else None)
    def live_panel():
        playing = bool(ss.is_playing and ss.session_start_ts)
        if playing:
            heartbeat_tick()
            cur = ss.session_course
            stats = ss.base_stats
            live_cid, live_sec = cur["id"], ss.current_session_sec
        else:
            cur = courses_by_id[selected_id]
            stats = get_course_stats(reg)
            live_cid, live_sec = None, 0

        target = cur["target_min"] * 60
        total = stats.get(cur["id"], {}).get("sec", 0) + live_sec

        col1, col2 = st.columns([1, 2])
        with col1:
            st.markdown(f"### ⏱️ {cur['title']}")
            st.progress(min(total / target, 1.0))
            st.metric("누적 시청 시간", f"{fmt_sec(total)} / {cur['target_min']}분")
            st.caption("동일 계정으로는 한 번에 하나의 창에서만 시청 시간이 누적됩니다.")
        with col2:
            st.markdown("### 📝 이수 상태")
            if total >= target:
                st.success("🎉 이 과목의 필수 시청 시간을 모두 충족하여 이수가 완료되었습니다!")
            else:
                st.info(f"목표 시간까지 **{fmt_sec(target - total)}** 남았습니다.")
            render_backup_status(backup)

        st.markdown("---")
        st.subheader(f"📊 [{current_user['name']} 님]의 과목별 이수 현황")
        dash = build_dashboard_df(enabled_courses, stats, live_cid, live_sec)
        done_n = int((dash["상태"] == "✅ 이수 완료").sum())
        st.metric("이수 완료 과목", f"{done_n} / {len(dash)}")
        st.dataframe(
            dash,
            hide_index=True,
            column_config={
                "진행률": st.column_config.ProgressColumn(
                    "진행률", min_value=0, max_value=100, format="%d%%"),
            },
        )

    live_panel()

    # ---- 개인 시청 이력 ----
    st.markdown("---")
    st.subheader(f"🗂 [{current_user['name']} 님]의 시청 이력")
    user_logs_df = get_user_logs(reg)
    if not user_logs_df.empty:
        user_logs_df["과목"] = user_logs_df.apply(
            lambda r: r["course_title"] or courses_by_id.get(r["course_id"], {}).get(
                "title", f"과목 {r['course_id']}"), axis=1)
        user_logs_df["시청 시간"] = user_logs_df["session_seconds"].apply(fmt_sec)
        user_logs_df = user_logs_df.rename(columns={
            "session_start_time": "시청 시작 시각 (KST)",
            "session_end_time": "시청 종료/저장 시각 (KST)",
        })[["과목", "시청 시작 시각 (KST)", "시청 종료/저장 시각 (KST)", "시청 시간"]]
        st.dataframe(user_logs_df, hide_index=True)
        st.caption("※ 진행 중인 세션은 '일시 정지 및 저장'을 누르면 이 표에 반영됩니다.")
    else:
        st.info("아직 저장된 시청 이력이 없습니다. 영상 시청을 시작하시면 기록이 생성됩니다.")
