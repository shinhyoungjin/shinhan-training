import datetime
import sqlite3
import time
from datetime import datetime as dt
import gspread
from google.oauth2.service_account import Credentials
import pandas as pd
import pytz
import streamlit as st

# --- 한국 시간대(KST) 설정 ---
def get_kst_now_str():
    kst = pytz.timezone('Asia/Seoul')
    return dt.now(kst).strftime('%Y-%m-%d %H:%M:%S')

# --- 구글 시트 백업 연동 클라이언트 ---
def get_gspread_client():
    try:
        if "gcp_service_account" in st.secrets:
            scopes = [
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive"
            ]
            creds = Credentials.from_service_account_info(
                st.secrets["gcp_service_account"],
                scopes=scopes
            )
            return gspread.authorize(creds)
    except Exception as e:
        st.error(f"❌ GCP 서비스 계정 인증 중 예외 발생: {e}")
    return None

# ================= =========================================
# 개선된 구글 시트 백업 기능 (방식 A: 접속 세션별 1행 생성 & 30초 주기 갱신)
# ================= =========================================
def backup_to_google_sheet_session(reg_num, name, email, start_time, end_time, session_sec, is_final_save=False):
    """
    1차/2차 등 새로 접속할 때마다 별도의 세션 행(Row)을 추가하고,
    동일 세션 안에서는 30초마다 해당 행의 시청 시간만 덮어씌워 갱신합니다.
    """
    now = time.time()
    last_saved = st.session_state.get("last_gsheet_save_time", 0)

    # 수동 일시정지(is_final_save)가 아니면 30초 간격 제한 적용
    if not is_final_save and (now - last_saved < 30) and ("gsheet_row_num" in st.session_state):
        return True

    try:
        client = get_gspread_client()
        if client is None or "backup_sheet_url" not in st.secrets:
            return False

        spreadsheet = client.open_by_url(st.secrets["backup_sheet_url"])
        sheet = spreadsheet.sheet1
        time_str = f"{session_sec // 60}분 {session_sec % 60}초"
        status_str = "시청 완료" if is_final_save else "시청 중 (자동저장)"

        # ① 최초 1회만 새로운 줄 생성 (새 세션 ID 발급)
        if "gsheet_row_num" not in st.session_state:
            session_id = f"{reg_num}_{dt.now().strftime('%Y%m%d_%H%M%S')}"
            new_row = [session_id, reg_num, name, email, start_time, end_time, session_sec, time_str, status_str]
            sheet.append_row(new_row)
            
            all_values = sheet.get_all_values()
            st.session_state["gsheet_row_num"] = len(all_values)
            st.session_state["last_gsheet_save_time"] = now
            return True

        # ② 기존 생성된 세션 행이 존재할 경우 해당 줄만 덮어쓰기 (기존 1차 시청 기록 보존)
        row_num = st.session_state["gsheet_row_num"]
        sheet.update_cell(row_num, 5, end_time)     # 5번째 열: 종료/저장 시각
        sheet.update_cell(row_num, 6, session_sec)  # 6번째 열: 시청 초
        sheet.update_cell(row_num, 7, time_str)     # 7번째 열: 포맷팅된 시청 시간
        sheet.update_cell(row_num, 9, status_str)   # 9번째 열: 이수/시청 상태
        
        st.session_state["last_gsheet_save_time"] = now
        return True

    except Exception as e:
        print(f"구글 백업 업데이트 중 일시적 예외: {e}")
        return False

# --- 1. 페이지 기본 설정 ---
st.set_page_config(
    page_title="법인 임직원 온라인 교육 시스템",
    page_icon="🎓",
    layout="wide"
)

# --- 2. 데이터베이스(SQLite) 초기화 ---
def init_db():
    conn = sqlite3.connect('training_data.db')
    c = conn.cursor()
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            id INTEGER PRIMARY KEY,
            video_url TEXT,
            target_minutes INTEGER,
            admin_password TEXT
        )
    ''')
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS watch_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            registration_number TEXT,
            name TEXT,
            email TEXT,
            session_start_time TEXT,
            session_end_time TEXT,
            session_seconds INTEGER
        )
    ''')

    c.execute("SELECT COUNT(*) FROM settings")
    if c.fetchone()[0] == 0:
        c.execute("INSERT INTO settings VALUES (1, 'https://www.youtube.com/watch?v=dQw4w9WgXcQ', 50, 'admin1234')")
        
    conn.commit()
    conn.close()

init_db()

def get_settings():
    conn = sqlite3.connect('training_data.db')
    c = conn.cursor()
    c.execute("SELECT video_url, target_minutes, admin_password FROM settings WHERE id = 1")
    row = c.fetchone()
    conn.close()
    return {"url": row[0], "target_min": row[1], "password": row[2]}

def get_user_total_seconds(reg_num):
    conn = sqlite3.connect('training_data.db')
    c = conn.cursor()
    c.execute("SELECT SUM(session_seconds) FROM watch_logs WHERE registration_number = ?", (reg_num,))
    result = c.fetchone()[0]
    conn.close()
    return result if result is not None else 0

def get_user_logs(reg_num):
    conn = sqlite3.connect('training_data.db')
    query = """
        SELECT session_start_time, session_end_time, session_seconds 
        FROM watch_logs 
        WHERE registration_number = ? 
        ORDER BY id DESC
    """
    df = pd.read_sql(query, conn, params=(reg_num,))
    conn.close()
    return df

def upsert_watch_session(log_id, reg_num, name, email, start_time, end_time, session_sec):
    conn = sqlite3.connect('training_data.db')
    c = conn.cursor()
    
    if log_id is None:
        c.execute('''
            INSERT INTO watch_logs (registration_number, name, email, session_start_time, session_end_time, session_seconds)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (reg_num, name, email, start_time, end_time, session_sec))
        new_id = c.lastrowid
    else:
        c.execute('''
            UPDATE watch_logs 
            SET session_end_time = ?, session_seconds = ?
            WHERE id = ?
        ''', (end_time, session_sec, log_id))
        new_id = log_id
        
    conn.commit()
    conn.close()
    return new_id

# --- 3. 명단 구글 시트 연동 ---
@st.cache_data(ttl=600)
def get_users_from_google_sheet():
    sheet_url = "https://docs.google.com/spreadsheets/d/1kC87Ec4T2S0gGu28vI_Hzt5THhXuvZPK1P88hfeFEYI/export?format=csv&gid=0"
    try:
        df = pd.read_csv(sheet_url)
        df['등록번호'] = df['등록번호'].fillna('').astype(str).str.replace('.0', '', regex=False)
        df['성명'] = df['성명'].fillna('')
        df['rsm 메일'] = df['rsm 메일'].fillna('')
        
        df = df.rename(columns={
            '등록번호': 'registration_number',
            '성명': 'name',
            'rsm 메일': 'email'
        })
        return df[['registration_number', 'name', 'email']]
    except Exception as e:
        st.error(f"구글 명단 시트를 불러오는 중 오류가 발생했습니다: {e}")
        return pd.DataFrame(columns=['registration_number', 'name', 'email'])

# --- 4. 세션 상태 초기화 ---
if 'is_playing' not in st.session_state:
    st.session_state.is_playing = False
if 'current_session_sec' not in st.session_state:
    st.session_state.current_session_sec = 0
if 'selected_reg_num' not in st.session_state:
    st.session_state.selected_reg_num = None
if 'last_autosave_time' not in st.session_state:
    st.session_state.last_autosave_time = time.time()
if 'session_start_str' not in st.session_state:
    st.session_state.session_start_str = None
if 'current_log_id' not in st.session_state:
    st.session_state.current_log_id = None

settings = get_settings()
target_seconds = settings["target_min"] * 60

# --- 5. 메인 UI ---
st.title("🎓 법인 임직원 법정의무/자체 온라인 교육")
st.caption("시청 완료 조건: 지정된 누적 시간 이상 시청 시 자동 이수 완료 (구글 드라이브 실시간 이중 백업 연동)")

# 사이드바
st.sidebar.header("👤 수강자 확인")
users_df = get_users_from_google_sheet()

current_user = None
if not users_df.empty:
    user_options = [
        f"{row['name']} (등록번호: {row['registration_number']}) - {row['email']}" 
        for _, row in users_df.iterrows()
        if row['name'] != ''
    ]
    
    selected_user_str = st.sidebar.selectbox(
        "본인의 이름을 검색하여 선택하세요 (오탈자 방지)",
        options=["선택하세요"] + user_options
    )

    if selected_user_str != "선택하세요":
        reg_num = selected_user_str.split('등록번호: ')[1].split(')')[0]
        
        if st.session_state.selected_reg_num != reg_num:
            st.session_state.selected_reg_num = reg_num
            st.session_state.is_playing = False
            st.session_state.current_session_sec = 0
            st.session_state.session_start_str = None
            st.session_state.current_log_id = None
            if "gsheet_row_num" in st.session_state:
                del st.session_state["gsheet_row_num"]
            
        current_user = users_df[users_df['registration_number'] == reg_num].iloc[0]
        st.sidebar.success(f"확인됨: **{current_user['name']}** 님")

st.sidebar.markdown("---")

# 관리자 메뉴
with st.sidebar.expander("⚙️ 관리자 메뉴"):
    admin_pw = st.text_input("관리자 비밀번호", type="password")
    if admin_pw == settings["password"]:
        st.success("관리자 인증 성공")
        
        st.subheader("1. 교육 영상 및 시간 교체")
        new_url = st.text_input("유튜브 영상 URL", value=settings["url"])
        new_target = st.number_input("목표 시청시간(분)", value=settings["target_min"], min_value=1)
        if st.button("설정 저장"):
            conn = sqlite3.connect('training_data.db')
            c = conn.cursor()
            c.execute("UPDATE settings SET video_url = ?, target_minutes = ? WHERE id = 1", (new_url, new_target))
            conn.commit()
            conn.close()
            st.success("저장되었습니다!")
            st.rerun()

        st.subheader("2. 시청 기록 다운로드")
        conn = sqlite3.connect('training_data.db')
        logs_df = pd.read_sql("SELECT * FROM watch_logs", conn)
        conn.close()
        
        if not logs_df.empty:
            summary_df = logs_df.groupby(['registration_number', 'name', 'email']).agg(
                총_시청_초=('session_seconds', 'sum'),
                시청_횟수=('id', 'count'),
                최초_시청일시=('session_start_time', 'min'),
                최종_시청일시=('session_end_time', 'max')
            ).reset_index()

            summary_df['총_시청_시간'] = summary_df['총_시청_초'].apply(lambda x: f"{x // 60}분 {x % 60}초")
            summary_df['이수_완료_여부'] = summary_df['총_시청_초'].apply(
                lambda x: '완료' if x >= settings['target_min'] * 60 else '미완료(진행중)'
            )
            
            summary_export = summary_df.rename(columns={
                'registration_number': '등록번호',
                'name': '성명',
                'email': '이메일'
            })[['등록번호', '성명', '이메일', '총_시청_시간', '이수_완료_여부', '시청_횟수', '최초_시청일시', '최종_시청일시']]

            csv_summary = summary_export.to_csv(index=False).encode('utf-8-sig')
            st.download_button(
                label="📥 1. 인별 총 시청 집계표 (요약) 다운로드",
                data=csv_summary,
                file_name=f"교육이수_요약집계표_{get_kst_now_str()[:10].replace('-','')}.csv",
                mime='text/csv'
            )

            logs_export = logs_df.rename(columns={
                'id': '로그ID',
                'registration_number': '등록번호',
                'name': '성명',
                'email': '이메일',
                'session_start_time': '시청시작시각(KST)',
                'session_end_time': '최종시청/저장시각(KST)',
                'session_seconds': '해당세션_시청초'
            })
            logs_export['해당세션_시청시간'] = logs_export['해당세션_시청초'].apply(lambda x: f"{x // 60}분 {x % 60}초")
            logs_export = logs_export[['로그ID', '등록번호', '성명', '이메일', '시청시작시각(KST)', '최종시청/저장시각(KST)', '해당세션_시청시간']]

            csv_logs = logs_export.to_csv(index=False).encode('utf-8-sig')
            st.download_button(
                label="📥 2. 개별 시청 상세 이력 로그 다운로드",
                data=csv_logs,
                file_name=f"개별_시청상세로그_{get_kst_now_str()[:10].replace('-','')}.csv",
                mime='text/csv'
            )
        else:
            st.info("아직 저장된 시청 기록이 없습니다.")

# 메인 교육 시청 영역
if current_user is None:
    st.warning("👈 왼쪽 사이드바에서 본인의 이름을 먼저 선택해 주세요.")
else:
    db_watched_sec = get_user_total_seconds(current_user['registration_number'])
    
    if st.session_state.current_log_id is not None:
        conn = sqlite3.connect('training_data.db')
        c = conn.cursor()
        c.execute("SELECT session_seconds FROM watch_logs WHERE id = ?", (st.session_state.current_log_id,))
        cur_row = c.fetchone()
        conn.close()
        cur_log_sec = cur_row[0] if cur_row else 0
        total_watched_sec = (db_watched_sec - cur_log_sec) + st.session_state.current_session_sec
    else:
        total_watched_sec = db_watched_sec + st.session_state.current_session_sec

    st.subheader(f"📌 교육 영상 (목표 시청시간: {settings['target_min']}분)")
    st.video(settings["url"])
    
    col1, col2 = st.columns([1, 2])
    
    with col1:
        st.markdown("### ⏱️ 시청 시간 측정")
        
        if not st.session_state.is_playing:
            if st.button("▶️ 영상 시청 시작 / 재개", use_container_width=True):
                st.session_state.is_playing = True
                st.session_state.last_autosave_time = time.time()
                st.session_state.session_start_str = get_kst_now_str()
                st.session_state.current_log_id = None
                st.session_state.current_session_sec = 0
                
                # 새로 접속/재시작 시 구글 시트 행 추적 번호 초기화
                if "gsheet_row_num" in st.session_state:
                    del st.session_state["gsheet_row_num"]
                st.rerun()
        else:
            if st.button("⏸️ 일시 정지 및 DB 저장", use_container_width=True):
                if st.session_state.current_session_sec > 0:
                    end_str = get_kst_now_str()
                    
                    # 1. 로컬 SQLite DB 저장
                    upsert_watch_session(
                        st.session_state.current_log_id,
                        current_user['registration_number'],
                        current_user['name'],
                        current_user['email'],
                        st.session_state.session_start_str,
                        end_str,
                        st.session_state.current_session_sec
                    )
                    
                    # 2. 구글 백업 시트 최종 정지 저장
                    backup_to_google_sheet_session(
                        current_user['registration_number'],
                        current_user['name'],
                        current_user['email'],
                        st.session_state.session_start_str,
                        end_str,
                        st.session_state.current_session_sec,
                        is_final_save=True
                    )

                st.session_state.is_playing = False
                st.session_state.current_session_sec = 0
                st.session_state.current_log_id = None
                st.session_state.session_start_str = None
                if "gsheet_row_num" in st.session_state:
                    del st.session_state["gsheet_row_num"]
                st.rerun()

        progress = min(total_watched_sec / target_seconds, 1.0)
        st.progress(progress)
        
        current_min = total_watched_sec // 60
        current_sec = total_watched_sec % 60
        st.metric("총 누적 시청 시간", f"{current_min}분 {current_sec}초 / {settings['target_min']}분")
        st.caption("🔒 시청 시간은 실시간 자동 저장되며, 구글 드라이브에 안전하게 이중 백업됩니다.")

    with col2:
        st.markdown("### 📝 이수 상태")
        if total_watched_sec >= target_seconds:
            st.success("🎉 필수 시청 시간을 모두 충족하여 이수가 완료되었습니다!")
            st.info("관리자 제출용 DB 및 구글 드라이브에 이수 기록이 안전하게 백업되었습니다.")
        else:
            remaining_sec = target_seconds - total_watched_sec
            st.info(f"목표 시간까지 **{remaining_sec // 60}분 {remaining_sec % 60}초** 남았습니다.")

    # 수강자 전용 개인 시청 이력 영역
    st.markdown("---")
    st.subheader(f"📊 [{current_user['name']} 님]의 개인 교육 이수 현황")
    
    user_logs_df = get_user_logs(current_user['registration_number'])
    
    if not user_logs_df.empty:
        user_logs_df['시청 시간'] = user_logs_df['session_seconds'].apply(lambda x: f"{x // 60}분 {x % 60}초")
        user_logs_df = user_logs_df.rename(columns={
            'session_start_time': '시청 시작 시각 (KST)',
            'session_end_time': '시청 종료/저장 시각 (KST)'
        })[['시청 시작 시각 (KST)', '시청 종료/저장 시각 (KST)', '시청 시간']]
        
        st.dataframe(user_logs_df, use_container_width=True)
    else:
        st.info("아직 저장된 시청 이력이 없습니다. 영상 시청을 시작하시면 기록이 생성됩니다.")

    # 1초 타이머 + 5초 로컬 DB 자동 업데이트 + 30초 구글 백업 시트 갱신
    if st.session_state.is_playing:
        time.sleep(1)
        st.session_state.current_session_sec += 1
        
        now = time.time()
        end_str = get_kst_now_str()
        
        # 5초마다 로컬 SQLite DB 상시 업데이트
        if now - st.session_state.last_autosave_time >= 5:
            log_id = upsert_watch_session(
                st.session_state.current_log_id,
                current_user['registration_number'],
                current_user['name'],
                current_user['email'],
                st.session_state.session_start_str,
                end_str,
                st.session_state.current_session_sec
            )
            st.session_state.current_log_id = log_id
            st.session_state.last_autosave_time = now

        # 구글 백업 시트에 30초 간격으로 현재 세션 행에만 시청 시간 덮어쓰기
        backup_to_google_sheet_session(
            current_user['registration_number'],
            current_user['name'],
            current_user['email'],
            st.session_state.session_start_str,
            end_str,
            st.session_state.current_session_sec,
            is_final_save=False
        )

        st.rerun()
