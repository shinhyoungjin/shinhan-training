import streamlit as st
import pandas as pd
import sqlite3
from datetime import datetime
import time

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
    
    # 설정 테이블
    c.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            id INTEGER PRIMARY KEY,
            video_url TEXT,
            target_minutes INTEGER,
            admin_password TEXT
        )
    ''')
    
    # 시청 기록 테이블 (기존 테이블 초기화 및 새로 생성)
    c.execute("DROP TABLE IF EXISTS watch_records")
    c.execute('''
        CREATE TABLE IF NOT EXISTS watch_records (
            registration_number TEXT PRIMARY KEY,
            name TEXT,
            email TEXT,
            total_watched_seconds INTEGER,
            first_start_time TEXT,
            last_update_time TEXT,
            is_completed INTEGER
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

# DB에서 사용자의 기존 시청 정보 조회
def get_user_record(reg_num):
    conn = sqlite3.connect('training_data.db')
    c = conn.cursor()
    try:
        c.execute("SELECT total_watched_seconds, first_start_time, is_completed FROM watch_records WHERE registration_number = ?", (reg_num,))
        row = c.fetchone()
        conn.close()
        if row:
            return {"total_sec": row[0], "first_start": row[1], "is_completed": row[2]}
    except Exception:
        conn.close()
    return {"total_sec": 0, "first_start": None, "is_completed": 0}

# 실시간 시청 시간 갱신 (Auto-save) - REPLACE INTO 구문으로 변경
def update_user_watched_time(reg_num, name, email, add_seconds, target_seconds):
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    record = get_user_record(reg_num)
    
    new_total_sec = record["total_sec"] + add_seconds
    first_start = record["first_start"] if record["first_start"] else now_str
    is_completed = 1 if new_total_sec >= target_seconds else 0

    conn = sqlite3.connect('training_data.db')
    c = conn.cursor()
    try:
        c.execute('''
            REPLACE INTO watch_records (registration_number, name, email, total_watched_seconds, first_start_time, last_update_time, is_completed)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (reg_num, name, email, new_total_sec, first_start, now_str, is_completed))
        conn.commit()
    except Exception as e:
        st.error(f"저장 오류: {e}")
    finally:
        conn.close()
    return new_total_sec, is_completed

# --- 3. 구글 시트 데이터 가져오기 ---
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
        st.error(f"구글 시트를 불러오는 중 오류가 발생했습니다: {e}")
        return pd.DataFrame(columns=['registration_number', 'name', 'email'])

# --- 4. 세션 상태 초기화 ---
if 'is_playing' not in st.session_state:
    st.session_state.is_playing = False
if 'last_autosave_time' not in st.session_state:
    st.session_state.last_autosave_time = time.time()
if 'selected_reg_num' not in st.session_state:
    st.session_state.selected_reg_num = None

settings = get_settings()
target_seconds = settings["target_min"] * 60

# --- 5. 메인 UI ---
st.title("🎓 법인 임직원 법정의무/자체 온라인 교육")
st.caption("시청 완료 조건: 지정된 누적 시간 이상 시청 시 자동 이수 완료 (실시간 자동 저장 지원)")

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
            st.session_state.last_autosave_time = time.time()
            
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
        records_df = pd.read_sql("SELECT * FROM watch_records", conn)
        conn.close()
        
        if not records_df.empty:
            records_df['총_시청_시간'] = records_df['total_watched_seconds'].apply(lambda x: f"{x // 60}분 {x % 60}초")
            records_df['이수_완료_여부'] = records_df['is_completed'].apply(lambda x: '완료' if x == 1 else '미완료(진행중)')
            
            export_df = records_df.rename(columns={
                'registration_number': '등록번호',
                'name': '성명',
                'email': '이메일',
                'first_start_time': '최초시청일시',
                'last_update_time': '최종시청일시'
            })[['등록번호', '성명', '이메일', '총_시청_시간', '이수_완료_여부', '최초시청일시', '최종시청일시']]

            csv_data = export_df.to_csv(index=False).encode('utf-8-sig')
            st.download_button(
                label="📥 교육 이수 현황 (CSV/엑셀) 다운로드",
                data=csv_data,
                file_name=f"교육이수현황_{datetime.now().strftime('%Y%m%d')}.csv",
                mime='text/csv'
            )
        else:
            st.info("아직 저장된 시청 기록이 없습니다.")

# 메인 교육 시청 영역
if current_user is None:
    st.warning("👈 왼쪽 사이드바에서 본인의 이름을 먼저 선택해 주세요.")
else:
    user_record = get_user_record(current_user['registration_number'])
    total_watched_sec = user_record["total_sec"]

    st.subheader(f"📌 교육 영상 (목표 시청시간: {settings['target_min']}분)")
    st.video(settings["url"])
    
    col1, col2 = st.columns([1, 2])
    
    with col1:
        st.markdown("### ⏱️ 시청 시간 측정")
        
        if not st.session_state.is_playing:
            if st.button("▶️ 영상 시청 시작 / 재개", use_container_width=True):
                st.session_state.is_playing = True
                st.session_state.last_autosave_time = time.time()
                st.rerun()
        else:
            if st.button("⏸️ 일시 정지", use_container_width=True):
                st.session_state.is_playing = False
                st.rerun()

        progress = min(total_watched_sec / target_seconds, 1.0)
        st.progress(progress)
        
        current_min = total_watched_sec // 60
        current_sec = total_watched_sec % 60
        st.metric("총 누적 시청 시간", f"{current_min}분 {current_sec}초 / {settings['target_min']}분")
        st.caption("🔒 시청 기록은 10초마다 DB에 실시간으로 자동 저장됩니다.")

    with col2:
        st.markdown("### 📝 이수 상태")
        if total_watched_sec >= target_seconds:
            st.success("🎉 필수 시청 시간을 모두 충족하여 이수가 완료되었습니다!")
            st.info("관리자 제출용 DB에 이수 완료 상태가 자동으로 누적되었습니다.")
        else:
            remaining_sec = target_seconds - total_watched_sec
            st.info(f"목표 시간까지 **{remaining_sec // 60}분 {remaining_sec % 60}초** 남았습니다.")

    if st.session_state.is_playing:
        time.sleep(1)
        now = time.time()
        if now - st.session_state.last_autosave_time >= 10:
            update_user_watched_time(
                current_user['registration_number'],
                current_user['name'],
                current_user['email'],
                10,
                target_seconds
            )
            st.session_state.last_autosave_time = now
        st.rerun()
