import streamlit as st
import sqlite3
import pandas as pd
from datetime import datetime
import time
import json
import gspread
from google.oauth2.service_account import Credentials

# ==========================================
# 1. DB 설정 및 초기화
# ==========================================
DB_FILE = "watch_history.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    # 개별 시청 상세 이력 테이블 (session_id 포함)
    c.execute('''
        CREATE TABLE IF NOT EXISTS watch_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            user_id TEXT,
            user_name TEXT,
            session_start TEXT,
            last_watch_time TEXT,
            session_duration INTEGER,
            total_duration INTEGER,
            status TEXT
        )
    ''')
    conn.commit()
    conn.close()

init_db()

# ==========================================
# 2. 구글 시트 연동 함수 (최신 google-auth 적용)
# ==========================================
def get_gspread_client():
    try:
        scope = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ]
        creds_dict = json.loads(st.secrets["gcp_service_account"])
        # 최신 google-auth 인증 방식
        creds = Credentials.from_service_account_info(creds_dict, scopes=scope)
        client = gspread.authorize(creds)
        return client
    except Exception as e:
        st.error(f"구글 시트 인증 실패: {e}")
        return None

def sync_db_log_to_sheet(log_id):
    """
    watch_logs DB에서 최신 데이터를 읽어 구글 시트에 1:1로 원자적 동기화합니다.
    """
    client = get_gspread_client()
    if not client:
        return

    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("""
            SELECT id, session_id, user_id, user_name, session_start, last_watch_time, session_duration, total_duration, status
            FROM watch_logs WHERE id = ?
        """, (log_id,))
        row = c.fetchone()
        conn.close()

        if not row:
            return

        sheet = client.open_by_key(st.secrets["spreadsheet_id"]).sheet1
        records = sheet.get_all_values()
        
        # 시간 포맷팅 헬퍼
        def format_sec(sec):
            m, s = divmod(sec, 60)
            h, m = divmod(m, 60)
            if h > 0:
                return f"{h}시간 {m}분 {s}초 ({sec}초)"
            return f"{m}분 {s}초 ({sec}초)"

        log_data = [
            str(row[0]),                  # Log ID
            str(row[1]),                  # 세션 ID
            str(row[2]),                  # 사번/ID
            str(row[3]),                  # 이름
            str(row[4]),                  # 최초 접속/시작 시각
            str(row[5]),                  # 최종 시청/저장 시각
            format_sec(row[6]),           # 이번 세션 시청시간
            format_sec(row[7]),           # 총 누적 시청시간 (UI 메트릭과 100% 동일)
            str(row[8])                   # 상태 (시청 중 / 완강)
        ]

        # 헤더 자동 생성 및 보정 (A~I열)
        headers = ["Log ID", "세션 ID", "사번", "이름", "최초시작시각", "최종시청시각", "이번세션시청시간", "총누적시청시간", "상태"]
        if not records:
            sheet.append_row(headers)
            records = [headers]
        elif records[0] != headers:
            sheet.update("A1:I1", [headers])

        # 기존 Log ID 행 탐색
        row_index = None
        for idx, r in enumerate(records[1:], start=2):
            if len(r) > 0 and r[0] == str(log_id):
                row_index = idx
                break

        if row_index:
            # 기존 행 업데이트
            sheet.update(f"A{row_index}:I{row_index}", [log_data])
        else:
            # 신규 행 추가
            sheet.append_row(log_data)

    except Exception as e:
        print(f"구글 시트 동기화 오류: {e}")

# ==========================================
# 3. DB 로깅 헬퍼 함수
# ==========================================
def create_new_watch_log(session_id, user_id, user_name, total_duration):
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        INSERT INTO watch_logs (session_id, user_id, user_name, session_start, last_watch_time, session_duration, total_duration, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (session_id, user_id, user_name, now_str, now_str, 0, total_duration, "시청 중"))
    log_id = c.lastrowid
    conn.commit()
    conn.close()
    
    sync_db_log_to_sheet(log_id)
    return log_id

def update_watch_log(log_id, session_sec, total_sec, completed=False):
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    status = "완강 (수료)" if completed else "시청 중"
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        UPDATE watch_logs
        SET last_watch_time = ?,
            session_duration = ?,
            total_duration = ?,
            status = ?
        WHERE id = ?
    """, (now_str, session_sec, total_sec, status, log_id))
    conn.commit()
    conn.close()

    sync_db_log_to_sheet(log_id)

# ==========================================
# 4. Streamlit UI
# ==========================================
st.set_page_config(page_title="동영상 학습 시스템", layout="wide")

sidebar_mode = st.sidebar.radio("메뉴 선택", ["동영상 시청", "관리자 메뉴"])

if sidebar_mode == "동영상 시청":
    st.title("📺 필수 교육 동영상 시청")

    col1, col2 = st.columns(2)
    with col1:
        user_id = st.text_input("사번 / ID", value=st.session_state.get("user_id", ""))
    with col2:
        user_name = st.text_input("이름", value=st.session_state.get("user_name", ""))

    if user_id and user_name:
        st.session_state["user_id"] = user_id
        st.session_state["user_name"] = user_name

        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT MAX(total_duration) FROM watch_logs WHERE user_id = ? AND status = '완강 (수료)'", (user_id,))
        past_record = c.fetchone()[0]
        base_prior_duration = past_record if past_record else 0
        conn.close()

        if "current_log_id" not in st.session_state or st.session_state.get("active_user") != user_id:
            timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
            session_id = f"{user_id}_{timestamp_str}"
            
            st.session_state["active_user"] = user_id
            st.session_state["session_id"] = session_id
            st.session_state["session_start_time"] = time.time()
            st.session_state["prior_duration"] = base_prior_duration
            
            log_id = create_new_watch_log(session_id, user_id, user_name, base_prior_duration)
            st.session_state["current_log_id"] = log_id

        elapsed_session_sec = int(time.time() - st.session_state["session_start_time"])
        total_accumulated_sec = st.session_state["prior_duration"] + elapsed_session_sec

        TARGET_SEC = 3000
        is_completed = total_accumulated_sec >= TARGET_SEC

        st.subheader("⏱️ 나의 학습 현황")
        st.caption(f"현재 세션 ID: `{st.session_state.get('session_id', '')}`")
        
        m_col1, m_col2, m_col3 = st.columns(3)
        m_col1.metric("이번 세션 시청시간", f"{elapsed_session_sec // 60}분 {elapsed_session_sec % 60}초")
        m_col2.metric("총 누적 시청시간", f"{total_accumulated_sec // 60}분 {total_accumulated_sec % 60}초")
        m_col3.metric("이수 상태", "✅ 완강 (50분 달성)" if is_completed else "⏳ 진행 중")

        progress_val = min(1.0, total_accumulated_sec / TARGET_SEC)
        st.progress(progress_val)

        st.video("https://www.youtube.com/watch?v=dQw4w9WgXcQ")

        update_watch_log(
            st.session_state["current_log_id"],
            elapsed_session_sec,
            total_accumulated_sec,
            completed=is_completed
        )

        time.sleep(1)
        st.rerun()

    else:
        st.info("사번과 이름을 입력하셔야 시청 기록이 저장됩니다.")

elif sidebar_mode == "관리자 메뉴":
    st.title("🛠️️ 관리자 메뉴")
    
    tab1, tab2 = st.tabs(["1. 사용자별 최종 요약", "2. 시청 기록 다운로드 (개별 상세 이력)"])

    with tab1:
        st.subheader("📊 사용자별 최종 누적 시청 현황")
        conn = sqlite3.connect(DB_FILE)
        df_summary = pd.read_sql_query("""
            SELECT 
                user_id AS 사번,
                user_name AS 이름,
                MAX(total_duration) / 60 AS 총누적시청_분,
                MAX(total_duration) AS 총누적시청_초,
                MAX(last_watch_time) AS 최근시청시각,
                CASE WHEN MAX(total_duration) >= 3000 THEN '수료' ELSE '미수료' END AS 완료여부
            FROM watch_logs
            GROUP BY user_id, user_name
        """, conn)
        conn.close()
        st.dataframe(df_summary, use_container_width=True)

    with tab2:
        st.subheader("📜 개별 시청 상세 이력 로그 (세션 ID 포함)")
        conn = sqlite3.connect(DB_FILE)
        df_logs = pd.read_sql_query("""
            SELECT 
                id AS Log_ID,
                session_id AS 세션_ID,
                user_id AS 사번,
                user_name AS 이름,
                session_start AS 최초시작시각,
                last_watch_time AS 최종시청시각,
                session_duration AS 이번세션시청_초,
                total_duration AS 총누적시청_초,
                status AS 상태
            FROM watch_logs
            ORDER BY id DESC
        """, conn)
        conn.close()
        
        st.dataframe(df_logs, use_container_width=True)

        csv = df_logs.to_csv(index=False).encode('utf-8-sig')
        st.download_button(
            label="📥 개별 시청 상세 이력 CSV 다운로드",
            data=csv,
            file_name=f"watch_detail_logs_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
            mime="text/csv"
        )
