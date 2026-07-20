#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
매일 자동 실행 스크립트 (Windows, git.exe 없이 동작)
======================================================

git 프로그램 없이, GitHub REST API(HTTPS 요청)만으로 파일을 커밋합니다.
scraper.py를 그대로 불러와 실행한 뒤, 결과 파일(public/index.html,
data/summary_cache.json)을 GitHub에 직접 업로드합니다.

사용법:
    1) 이 파일을 저장소 폴더(scraper.py가 있는 폴더)에 둔다
    2) config.json 을 만들어서 아래 값들을 채운다 (github_secrets.json.template 참고)
    3) python run_daily.py 실행

작업 스케줄러(Task Scheduler)에 등록해서 매일 자동 실행할 수 있습니다.
"""

import base64
import json
import os
import sys

import requests

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "github_secrets.json")

FILES_TO_PUSH = [
    "public/index.html",
    "data/summary_cache.json",
]


def load_config():
    if not os.path.exists(CONFIG_PATH):
        print(f"[오류] {CONFIG_PATH} 파일이 없습니다. 먼저 설정 파일을 만드세요.")
        sys.exit(1)
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def push_file_to_github(local_path: str, repo_path: str, cfg: dict):
    """git 없이 GitHub Contents API로 파일 하나를 업로드/갱신한다."""
    owner = cfg["github_owner"]
    repo = cfg["github_repo"]
    branch = cfg.get("github_branch", "main")
    token = cfg["github_token"]

    api_url = f"https://api.github.com/repos/{owner}/{repo}/contents/{repo_path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }

    # 1. 기존 파일의 sha 값 확인 (없으면 새 파일로 간주)
    sha = None
    resp = requests.get(api_url, headers=headers, params={"ref": branch}, timeout=20)
    if resp.status_code == 200:
        sha = resp.json().get("sha")
    elif resp.status_code != 404:
        print(f"[경고] {repo_path}: 기존 파일 조회 실패 (status={resp.status_code}) - {resp.text[:200]}")

    # 2. 로컬 파일 읽어서 base64 인코딩
    full_local_path = os.path.join(SCRIPT_DIR, local_path)
    if not os.path.exists(full_local_path):
        print(f"[경고] {full_local_path} 파일이 없어 건너뜁니다")
        return False

    with open(full_local_path, "rb") as f:
        content_b64 = base64.b64encode(f.read()).decode("utf-8")

    # 3. 업로드 (신규 생성 또는 갱신)
    payload = {
        "message": f"chore: 자동 업데이트 ({repo_path})",
        "content": content_b64,
        "branch": branch,
    }
    if sha:
        payload["sha"] = sha

    put_resp = requests.put(api_url, headers=headers, json=payload, timeout=30)
    if put_resp.status_code in (200, 201):
        print(f"[OK] {repo_path} 업로드 완료")
        return True
    else:
        print(f"[오류] {repo_path} 업로드 실패 (status={put_resp.status_code}) - {put_resp.text[:300]}")
        return False


def main():
    cfg = load_config()

    # 필요한 API 키들을 환경변수로 주입 (scraper.py가 os.environ에서 읽어감)
    os.environ["NAVER_CLIENT_ID"] = cfg.get("naver_client_id", "")
    os.environ["NAVER_CLIENT_SECRET"] = cfg.get("naver_client_secret", "")
    os.environ["GROQ_API_KEY"] = cfg.get("groq_api_key", "")
    os.environ["ENABLE_AI_SUMMARIES"] = cfg.get("enable_ai_summaries", "true")

    # scraper.py 실행 (같은 폴더에 있어야 함)
    os.chdir(SCRIPT_DIR)
    sys.path.insert(0, SCRIPT_DIR)
    import scraper  # noqa: E402
    scraper.main()

    # 결과 파일들을 git 없이 GitHub에 직접 업로드
    any_pushed = False
    for path in FILES_TO_PUSH:
        ok = push_file_to_github(path, path, cfg)
        any_pushed = any_pushed or ok

    if any_pushed:
        print("[완료] GitHub 업로드 처리 끝")
    else:
        print("[정보] 업로드된 파일이 없습니다 (에러 로그 확인 필요)")


if __name__ == "__main__":
    main()
