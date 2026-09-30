import datetime
import time
import pandas as pd
import streamlit as st
import gspread
from google.oauth2.service_account import Credentials

# ================= =========================================
# 1. 페이지 설정
# ================= =========================================
st.set_page_config(
    page_title="온라인 교육 시청 시스템",
    page_icon="🎬",
    layout="wide"
)

# ================= =========================================
# 2. 구글 시트 연결 (st.secrets 사용 / 캐싱 적용)
# ================= =========================================
@st.cache_resource
def init_google_sheet():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    
    # Secrets의 [gcp_service_account] 계정 정보를 안전하게 읽어옴 (JSON 파일 불필요)
    if "gcp_service_account" not in st.secrets or "backup_sheet_url" not in st.secrets:
        st.error("Streamlit Secrets 설정(gcp_service_account 또는 backup_sheet_url)이 올바르지 않습니다.")
        st.stop()
        
    service_account_info = dict(st.secrets["gcp_service_account"])
    sheet_url = st.secrets["backup_sheet_url"]
    
    creds = Credentials.from_service_account_info(service_account_info, scopes=scopes)
    client = gspread.authorize(creds)
    
    # Secrets에 설정된 구글 시트 URL로 시트1 오픈
    sheet = client.open_by_url(sheet_url).sheet1
    return sheet

try:
    sheet = init_google_sheet()
except Exception as e:
    st.error(f"구글 시트 연결에 실패했습니다: {e}")
    st.stop()

# ================= =========================================
# 3. 구글 시트 자동 저장 함수 (방식 A: 세션별 1행 유지 & 30초 주기 Update)
# ================= =========================================
def sync_watch_log_to_sheet(user_id, user_name, video_title, current_time_str):
    now = time.time()
    last_saved = st.session_state.get("last_save_time", 0)
    
    # API 호출 제한 방지: 최소 30초 간격으로만 구글 시트 갱신
    if now - last_saved < 30 and "current_session_row" in st.session_state:
        return

    try:
        # ① 현재 세션의 행(Row) 번호가 없는 경우 (최초 진입 시 새로운 줄 생성)
        if "current_session_row" not in st.session_state:
            session_id = f"{user_id}_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}"
            login_time = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            
            # 구글 시트 맨 아래에 새 시청 세션 행 추가
            new_row = [session_id, user_name, video_title, login_time, current_time_str, "시청 중"]
            sheet.append_row(new_row)
            
            # 방금 추가된 행 번호 계산하여 세션 상태에 저장
            all_values = sheet.get_all_values()
            st.session_state["current_session_row"] = len(all_values)
            st.session_state["session_id"] = session_id
            st.session_state["last_save_time"] = now
            return

        # ② 이미 생성된 세션 행이 있는 경우 (해당 줄의 시청시간 및 상태 덮어쓰기)
        row_num = st.session_state["current_session_row"]
        
        # 5번째 열(E열): 시청시간, 6번째 열(F열): 상태
        sheet.update_cell(row_num, 5, current_time_str)
        sheet.update_cell(row_num, 6, "시청 중 (자동저장)")
        
        st.session_state["last_save_time"] = now

    except Exception as e:
        # 동시 접속으로 일시적 API 오류 발생 시 앱이 멈추지 않도록 예외 처리
        print(f"구글 시트 저장 중 일시적 오류 발생: {e}")

# ================= =========================================
# 4. Streamlit 하단 '개인 교육 이수 현황' 표 출력 함수
# ================= =========================================
def display_user_history(user_name):
    st.markdown("---")
    st.subheader(f"📋 {user_name} 님의 개인 교육 이수 현황")
    
    try:
        data = sheet.get_all_records()
        if not data:
            st.info("아직 등록된 시청 이력이 없습니다.")
            return

        df = pd.DataFrame(data)
        
        # '사용자' 열이 존재하는지 확인 후 필터링
        if '사용자' in df.columns:
            user_df = df[df['사용자'] == user_name]
        else:
            st.warning("구글 시트의 1번째 줄(헤더)에 '사용자' 열이 없습니다.")
            return
        
        if user_df.empty:
            st.info("등록된 시청 이력이 없습니다.")
        else:
            # 보기 깔끔하도록 필요한 열만 추출 및 최신순 정렬
            target_cols = [col for col in ['영상제목', '접속일시', '시청시간', '상태'] if col in user_df.columns]
            display_df = user_df[target_cols].iloc[::-1].reset_index(drop=True)
            st.dataframe(display_df, use_container_width=True)
            
    except Exception as e:
        st.warning(f"이수 현황을 불러오는 중 오류가 발생했습니다: {e}")

# ================= =========================================
# 5. 메인 앱 화면 구성
# ================= =========================================
def main():
    st.title("🎥 사내 필수 직무 교육 시스템")
    
    # 사이드바: 사용자 로그인 정보 입력 (테스트용)
    st.sidebar.header("👤 사용자 정보")
    user_name = st.sidebar.text_input("이름", value="신현지")
    user_id = st.sidebar.text_input("사번/ID", value="shin123")
    video_title = "2026년 필수 정보보호 및 안전교육"

    st.write(f"**수강자:** {user_name} ({user_id}) | **수강 과목:** {video_title}")
    
    # 비디오 플레이어 영역
    st.markdown("### 📺 교육 영상 플레이어")
    
    # 세션 상태에 시청 시간 모의 카운터 설정 (예시용)
    if "simulated_seconds" not in st.session_state:
        st.session_state["simulated_seconds"] = 0

    # 샘플 비디오
    st.video("https://www.w3schools.com/html/mov_bbb.mp4")
    
    # --- 시청 시간 측정 및 자동 저장 로직 (테스트용 인터랙션) ---
    col1, col2, col3 = st.columns([1, 1, 2])
    with col1:
        if st.button("▶️ 영상 시청 중 (30초 진행)"):
            st.session_state["simulated_seconds"] += 30
    with col2:
        if st.button("🔄 시청 세션 초기화"):
            if "current_session_row" in st.session_state:
                del st.session_state["current_session_row"]
            st.session_state["simulated_seconds"] = 0
            st.rerun()

    # 초 단위를 MM:SS 포맷으로 변환
    minutes = st.session_state["simulated_seconds"] // 60
    seconds = st.session_state["simulated_seconds"] % 60
    current_time_str = f"{minutes:02d}분 {seconds:02d}초"

    st.info(f"⏱️ 현재 측정된 시청 시간: **{current_time_str}**")

    # 영상 시청 시간이 존재할 경우 30초 주기로 구글 시트에 동기화
    if st.session_state["simulated_seconds"] > 0:
        sync_watch_log_to_sheet(user_id, user_name, video_title, current_time_str)

    # --- 하단: 개인 교육 이수 현황 표 출력 ---
    display_user_history(user_name)

if __name__ == "__main__":
    main()
