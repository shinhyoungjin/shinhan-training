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
    
    # 교육 영상 및 목표 시간 설정 테이블
    c.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            id INTEGER PRIMARY KEY,
            video_url TEXT,
            target_minutes INTEGER,
            admin_password TEXT
        )
    ''')
    
    # 시청 세션 세부 로그 테이블 (중간 시청 내역 기록)
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

    # 기본 영상 세팅 (최초 실행 시)
    c.execute("SELECT COUNT(*) FROM settings")
    if c.fetchone()[0] == 0:
        c.execute("INSERT INTO settings VALUES (1, 'https://www.youtube.com/watch?v=dQw4w9WgXcQ', 50, 'admin1234')")
        
    conn.commit()
    conn.close()

init_db()

# DB 조작 함수들
def get_settings():
    conn = sqlite3.connect('training_data.db')
    c = conn.cursor()
    c.execute("SELECT video_url, target_minutes, admin_password FROM settings WHERE id = 1")
    row = c.fetchone()
    conn.close()
    return {"url": row[0], "target_min": row[1], "password": row[2]}

# 특정 수강자의 기존 총 누적 시청 시간(초 단위) 조회
def get_user_total_watched_seconds(reg_num):
    conn = sqlite3.connect('training_data.db')
    c = conn.cursor()
    c.execute("SELECT SUM(session_seconds) FROM watch_logs WHERE registration_number = ?", (reg_num,))
    result = c.fetchone()[0]
    conn.close()
    return result if result is not None else 0

# 시청 세션 저장 함수 (일시정지, 완료, 차시별 기록)
def save_watch_session(reg_num, name, email, start_time, end_time, session_sec):
    if session_sec <= 0:
        return
    conn = sqlite3.connect('training_data.db')
    c = conn.cursor()
    c.execute('''
        INSERT INTO watch_logs (registration_number, name, email, session_start_time, session_end_time, session_seconds)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (reg_num, name, email, start_time, end_time, session_sec))
    conn.commit()
    conn.close()

# --- 3. 구글 시트 수강생 명단 실시간 연동 (10분 캐싱) ---
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
if 'current_session_seconds' not in st.session_state:
    st.session_state.current_session_seconds = 0
if 'session_start_time' not in st.session_state:
    st.session_state.session_start_time = None
if 'selected_reg_num' not in st.session_state:
    st.session_state.selected_reg_num = None

settings = get_settings()
target_seconds = settings["target_min"] * 60

# --- 5. UI 구성 ---
st.title("🎓 법인 임직원 법정의무/자체 온라인 교육")
st.caption("시청 완료 조건: 지정된 누적 시간 이상 시청 시 완료 처리 (분할 시청 가능)")

# 사이드바: 수강자 식별
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
        
        # 수강자가 변경된 경우 기존 시청 상태 리셋
        if st.session_state.selected_reg_num != reg_num:
            st.session_state.selected_reg_num = reg_num
            st.session_state.is_playing = False
            st.session_state.current_session_seconds = 0
            st.session_state.session_start_time = None
            
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

        st.subheader("2. 시청 기록 다운로드 (통합 증빙용)")
        conn = sqlite3.connect('training_data.db')
        logs_df = pd.read_sql("SELECT * FROM watch_logs", conn)
        conn.close()
        
        if not logs_df.empty:
            # 인별 총 누적 시간 및 완료 여부 집계
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

            # 증빙용 CSV 생성
            csv_summary = summary_df.to_csv(index=False).encode('utf-8-sig')
            st.download_button(
                label="📥 인별 총 시청 집계표 (요약) 다운로드",
                data=csv_summary,
                file_name=f"교육이수_집계표_{datetime.now().strftime('%Y%m%d')}.csv",
                mime='text/csv'
            )
            
            csv_logs = logs_df.to_csv(index=False).encode('utf-8-sig')
            st.download_button(
                label="📥 상세 접속/시청 로그 전체 다운로드",
                data=csv_logs,
                file_name=f"상세시청로그_{datetime.now().strftime('%Y%m%d')}.csv",
                mime='text/csv'
            )
        else:
            st.info("아직 저장된 시청 기록이 없습니다.")

# --- 6. 교육 시청 메인 영역 ---
if current_user is None:
    st.warning("👈 왼쪽 사이드바에서 본인의 이름을 먼저 선택해 주세요.")
else:
    # 기존 DB에 저장된 누적 시청 시간 조회
    previous_watched_sec = get_user_total_watched_seconds(current_user['registration_number'])
    total_watched_sec = previous_watched_sec + st.session_state.current_session_seconds

    st.subheader(f"📌 교육 영상 (목표 시청시간: {settings['target_min']}분)")
    st.video(settings["url"])
    
    col1, col2 = st.columns([1, 2])
    
    with col1:
        st.markdown("### ⏱️ 시청 시간 측정")
        
        # 재생/일시정지 버튼 조작
        if not st.session_state.is_playing:
            if st.button("▶️ 영상 시청 시작 / 재개", use_container_width=True):
                st.session_state.is_playing = True
                st.session_state.session_start_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                st.rerun()
        else:
            if st.button("⏸️ 일시 정지 및 시청 기록 저장", use_container_width=True):
                # 일시 정지 시 현재 세션 기록을 DB에 중간 저장
                end_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                save_watch_session(
                    current_user['registration_number'],
                    current_user['name'],
                    current_user['email'],
                    st.session_state.session_start_time,
                    end_time,
                    st.session_state.current_session_seconds
                )
                st.session_state.is_playing = False
                st.session_state.current_session_seconds = 0
                st.session_state.session_start_time = None
                st.success("중간 시청 기록이 DB에 안전하게 저장되었습니다.")
                st.rerun()

        # 진척도 계산
        progress = min(total_watched_sec / target_seconds, 1.0)
        st.progress(progress)
        
        current_min = total_watched_sec // 60
        current_sec = total_watched_sec % 60
        st.metric("총 누적 시청 시간", f"{current_min}분 {current_sec}초 / {settings['target_min']}분")
        
        if previous_watched_sec > 0:
            st.caption(f"💡 기존 누적 시청 기록: {previous_watched_sec // 60}분 {previous_watched_sec % 60}초")

    with col2:
        st.markdown("### 📝 이수 완료 상태")
        if total_watched_sec >= target_seconds:
            st.success("🎉 필수 시청 시간을 모두 충족했습니다!")
            
            # 이수 완료 시 세션 자동 저장 및 제출 안내
            if st.session_state.is_playing:
                end_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                save_watch_session(
                    current_user['registration_number'],
                    current_user['name'],
                    current_user['email'],
                    st.session_state.session_start_time,
                    end_time,
                    st.session_state.current_session_seconds
                )
                st.session_state.is_playing = False
                st.session_state.current_session_seconds = 0
                st.balloons()
            
            st.info("이수가 성공적으로 완료되어 인증기관 제출용 DB에 누적 기록되었습니다.")
        else:
            remaining_sec = target_seconds - total_watched_sec
            st.info(f"목표 시간까지 **{remaining_sec // 60}분 {remaining_sec % 60}초** 남았습니다. 나누어 시청하셔도 됩니다.")

    # 실시간 카운터 (시청 중일 때)
    if st.session_state.is_playing:
        time.sleep(1)
        st.session_state.current_session_seconds += 1
        st.rerun()
