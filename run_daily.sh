#!/bin/zsh
# ==============================================================
# 매일 자동 실행 스크립트
# - API 키를 별도의 안전한 파일(~/.scraper_secrets)에서 불러온다
# - scraper.py 실행
# - 변경사항이 있으면 깃허브에 자동 push
# ==============================================================

set -e  # 중간에 에러 나면 즉시 중단 (조용히 넘어가지 않도록)

# 1. API 키 불러오기 (이 파일은 저장소 밖, 홈 디렉토리에 따로 둔다 - git에 절대 안 올라감)
if [ -f "$HOME/.scraper_secrets" ]; then
  source "$HOME/.scraper_secrets"
else
  echo "[오류] $HOME/.scraper_secrets 파일이 없습니다. API 키를 먼저 설정하세요."
  exit 1
fi

# 2. 저장소 경로로 이동 (★ 본인 환경에 맞게 이 경로만 수정하세요)
REPO_DIR="$HOME/Desktop/sunjin_resource"
cd "$REPO_DIR"

# 3. 스크래퍼 실행
/Library/Frameworks/Python.framework/Versions/3.14/bin/python3 scraper.py

# 4. 변경사항 있으면 커밋 + 푸시 (변경 없으면 조용히 넘어감)
git add public/index.html data/summary_cache.json
if ! git diff --cached --quiet; then
  git commit -m "chore: 로컬 자동 업데이트 $(date '+%Y-%m-%d %H:%M')"
  git push
  echo "[완료] 새 내용 push 완료"
else
  echo "[정보] 변경사항 없음, push 생략"
fi
