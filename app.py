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
    # 교육 영상 정보 테이블
    c.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            id INTEGER PRIMARY KEY,
            video_url TEXT,
            target_minutes INTEGER,
            admin_password TEXT
        )
    ''')
    # 시청 기록 테이블
    c.execute('''
        CREATE TABLE IF NOT EXISTS watch_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            registration_number TEXT,
            name TEXT,
            email TEXT,
            start_time TEXT,
            end_time TEXT,
            watched_seconds INTEGER,
            is_completed INTEGER
        )
    ''')
    
    # 기본 영상 세팅 (최초 실행 시)
    c.execute("SELECT COUNT(*) FROM settings")
    if c.fetchone()[0] == 0:
        c.execute("INSERT INTO settings VALUES (1, 'https://www.youtube.com/watch?v=dQw4w9WgXcQ', 50, 'admin1234')")
        
    conn.commit()
    conn.close()

init_db()

# DB 읽기/저장 함수들
def get_settings():
    conn = sqlite3.connect('training_data.db')
    c = conn.cursor()
    c.execute("SELECT video_url, target_minutes, admin_password FROM settings WHERE id = 1")
    row = c.fetchone()
    conn.close()
    return {"url": row[0], "target_min": row[1], "password": row[2]}

def save_record(reg_num, name, email, start_time, end_time, watched_sec, is_completed):
    conn = sqlite3.connect('training_data.db')
    c = conn.cursor()
    c.execute('''
        INSERT INTO watch_records (registration_number, name, email, start_time, end_time, watched_seconds, is_completed)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (reg_num, name, email, start_time, end_time, watched_sec, 1 if is_completed else 0))
    conn.commit()
    conn.close()

# --- 3. 구글 시트 데이터 실시간 불러오기 (10분 주기 캐싱) ---
@st.cache_data(ttl=600)
def get_users_from_google_sheet():
    sheet_url = "https://docs.google.com/spreadsheets/d/1kC87Ec4T2S0gGu28vI_Hzt5THhXuvZPK1P88hfeFEYI/export?format=csv&gid=0"
    try:
        df = pd.read_csv(sheet_url)
        # 성명, 등록번호, rsm 메일 컬럼 추출 및 정리
        df['등록번호'] = df['등록번호'].fillna('').astype(str).str.replace('.0', '', regex=False)
        df['성명'] = df['성명'].fillna('')
        df['rsm 메일'] = df['rsm 메일'].fillna('')
        
        # 내부 표준 컬럼명으로 변경
        df = df.rename(columns={
            '등록번호': 'registration_number',
            '성명': 'name',
            'rsm 메일': 'email'
        })
        return df[['registration_number', 'name', 'email']]
    except Exception as e:
        st.error(f"구글 시트를 불러오는 중 오류가 발생했습니다: {e}")
        return pd.DataFrame(columns=['registration_number', 'name', 'email'])

# --- 4. 세션 상태 관리 ---
if 'is_playing' not in st.session_state:
    st.session_state.is_playing = False
if 'watched_seconds' not in st.session_state:
    st.session_state.watched_seconds = 0
if 'start_time' not in st.session_state:
    st.session_state.start_time = None

settings = get_settings()
target_seconds = settings["target_min"] * 60

# --- 5. 메인 화면 구성 ---
st.title("🎓 법인 임직원 법정의무/자체 온라인 교육")
st.caption("시청 완료 조건: 지정된 누적 시간 이상 시청 시 완료 처리")

# 사이드바 (수강자 식별 & 관리자 로그인)
st.sidebar.header("👤 수강자 확인")

# 구글 시트에서 명단 불러오기
users_df = get_users_from_google_sheet()

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

    current_user = None
    if selected_user_str != "선택하세요":
        # 선택된 문자열에서 등록번호 파싱
        reg_num = selected_user_str.split('등록번호: ')[1].split(')')[0]
        current_user = users_df[users_df['registration_number'] == reg_num].iloc[0]
        st.sidebar.success(f"확인됨: **{current_user['name']}** 님")
else:
    st.sidebar.error("수강자 명단을 불러오지 못했습니다. 구글 시트 공유 설정을 확인해 주세요.")

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
            csv = records_df.to_csv(index=False).encode('utf-8-sig')
            st.download_button(
                label="📥 시청 기록 (CSV/엑셀) 다운로드",
                data=csv,
                file_name=f"시청기록_{datetime.now().strftime('%Y%m%d')}.csv",
                mime='text/csv'
            )
        else:
            st.info("아직 저장된 시청 기록이 없습니다.")

# --- 6. 교육 시청 메인 영역 ---
if current_user is None:
    st.warning("👈 왼쪽 사이드바에서 본인의 이름을 먼저 선택해 주세요.")
else:
    st.subheader(f"📌 교육 영상 (목표 시청시간: {settings['target_min']}분)")
    
    # 유튜브 플레이어 표시
    st.video(settings["url"])
    
    col1, col2 = st.columns([1, 2])
    
    with col1:
        st.markdown("### ⏱️ 시청 시간 측정")
        
        # 타이머 조작 버튼
        if not st.session_state.is_playing:
            if st.button("▶️ 영상 시청 시작 / 재개", use_container_width=True):
                st.session_state.is_playing = True
                if st.session_state.start_time is None:
                    st.session_state.start_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                st.rerun()
        else:
            if st.button("⏸️ 일시 정지", use_container_width=True):
                st.session_state.is_playing = False
                st.rerun()

        # 진척도 및 누적 시간
        progress = min(st.session_state.watched_seconds / target_seconds, 1.0)
        st.progress(progress)
        
        current_min = st.session_state.watched_seconds // 60
        current_sec = st.session_state.watched_seconds % 60
        st.metric("누적 시청 시간", f"{current_min}분 {current_sec}초 / {settings['target_min']}분")

    with col2:
        st.markdown("### 📝 이수 제출")
        if st.session_state.watched_seconds >= target_seconds:
            st.success("🎉 필수 시청 시간을 모두 충족했습니다!")
            if st.button("✅ 시청 완료 기록 제출하기", type="primary", use_container_width=True):
                end_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                save_record(
                    current_user['registration_number'],
                    current_user['name'],
                    current_user['email'],
                    st.session_state.start_time,
                    end_time,
                    st.session_state.watched_seconds,
                    True
                )
                st.balloons()
                st.success("시청 완료 기록이 성공적으로 DB에 저장되었습니다.")
                # 상태 초기화
                st.session_state.watched_seconds = 0
                st.session_state.is_playing = False
                st.session_state.start_time = None
        else:
            st.info(f"목표 시간({settings['target_min']}분)을 채우시면 제출 버튼이 활성화됩니다.")

    # 타이머 실시간 카운트 루프 (시청 중일 때)
    if st.session_state.is_playing:
        time.sleep(1)
        st.session_state.watched_seconds += 1
        st.rerun()
