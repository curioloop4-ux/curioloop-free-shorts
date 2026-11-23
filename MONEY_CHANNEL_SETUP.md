# 돈좀아는척 채널 설정

## 기존 자원 재사용
다음 GitHub Actions secrets는 기존 DailyNibble 설정을 재사용합니다.
- GEMINI_API_KEY
- PIXABAY_API_KEY
- MONEY_YOUTUBE_CLIENT_ID: 새 웹 애플리케이션 OAuth 클라이언트 ID
- MONEY_YOUTUBE_CLIENT_SECRET: 새 웹 애플리케이션 OAuth 클라이언트 보안 비밀번호

돈좀아는척 채널은 새 웹 애플리케이션 OAuth 클라이언트를 사용하므로 다음 3개 Secret을 별도로 등록해야 합니다.
- MONEY_YOUTUBE_CLIENT_ID: 새 웹 애플리케이션 클라이언트 ID
- MONEY_YOUTUBE_CLIENT_SECRET: 새 웹 애플리케이션 클라이언트 보안 비밀번호
- MONEY_YOUTUBE_REFRESH_TOKEN: 돈좀아는척 채널을 선택해 발급한 OAuth refresh token

기존 YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET, YOUTUBE_REFRESH_TOKEN은 DailyNibble용이므로 덮어쓰지 마세요.

## 새 채널 OAuth 토큰 발급
1. 기존 Google Cloud 프로젝트 안에서 만든 웹 애플리케이션 OAuth 클라이언트를 사용합니다.
2. OAuth 인증을 진행할 때 돈좀아는척 채널에 접근 권한이 있는 Google 계정으로 로그인하고, 채널 선택 화면이 나오면 반드시 돈좀아는척 채널을 선택합니다.
3. YouTube Data API 업로드 범위 `https://www.googleapis.com/auth/youtube.upload`를 승인합니다.
4. 발급된 refresh token을 GitHub 저장소 Settings → Secrets and variables → Actions → New repository secret에서 `MONEY_YOUTUBE_REFRESH_TOKEN` 이름으로 저장합니다.
5. 새 웹 애플리케이션의 Client ID와 Client Secret도 각각 `MONEY_YOUTUBE_CLIENT_ID`, `MONEY_YOUTUBE_CLIENT_SECRET`으로 등록합니다. 기존 DailyNibble 인증 Secret은 변경하지 않습니다.

OAuth Playground를 사용하는 경우 Settings에서 Use your own OAuth credentials를 켜고 기존 Client ID와 Client Secret을 입력한 뒤 위 scope로 승인합니다. refresh token은 비밀번호와 동일하게 취급하고 채팅이나 코드에 붙여넣지 마세요.

참고: Google OAuth 동의 화면이 Testing 상태인 외부 앱은 일부 scope에서 refresh token이 7일 후 만료될 수 있습니다. 장기 자동화 전에는 OAuth 동의 화면의 게시 상태와 Google의 현재 정책을 확인해야 합니다.

## 실행
GitHub → Actions → MoneyKnowHow Korean Channel → Run workflow
- content_type: shorts 또는 longform
- output_only: 첫 테스트는 true
- privacy_status: unlisted

첫 실행은 MP4 생성만 진행합니다. Actions 실행 결과의 Artifacts에서 `money-channel-video`를 내려받아 영상·대본·출처를 확인하세요. 검수 후 업로드를 켜려면 output_only를 false로 설정하고, `MONEY_YOUTUBE_REFRESH_TOKEN`이 두 번째 채널용인지 확인합니다.

## 제작 기준
- Shorts: 한국어 35~55초 내외
- 롱폼: 한국어 경제 다큐멘터리 약 6~9분 목표
- 경제·금융 교육 목적이며 종목 추천이나 확정 수익을 약속하지 않음
- 영상은 Pixabay 검색 결과의 태그가 검색어와 맞는 경우에만 사용하고, 관련 영상이 부족하면 무관한 장면으로 채우지 않고 실패 처리
- Gemini가 제시하는 참고 URL은 자동으로 사실 검증되는 것이 아니므로 공개 업로드 전 반드시 열어 확인
