#!/bin/zsh
# 本機執行 youtube_watch.py（由 launchd 每 20 分鐘觸發，見 com.brian.tw-stock-agent.youtube-watch.plist）
# YouTube 會封鎖 GitHub Actions 等雲端機房 IP，家用網路不會，所以這支腳本改在本機跑。
# 已讀影片記錄 youtube_seen.json 仍 commit 回 GitHub，跟雲端版共用同一份狀態。

export PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
cd /Users/brian/tw-stock-agent || exit 1

echo "===== $(date '+%Y-%m-%d %H:%M:%S') ====="

git pull --rebase --autostash -q || echo "⚠️  git pull 失敗，用本機現有狀態繼續"

# yt-dlp 過舊會被 YouTube 回 403，每 7 天自動升級一次
STAMP=logs/.yt-dlp-upgraded
if [[ ! -f $STAMP || -n $(find $STAMP -mtime +7) ]]; then
  brew upgrade yt-dlp >/dev/null 2>&1 ; echo "🔄 yt-dlp 版本：$(yt-dlp --version)"
  touch $STAMP
fi

/usr/bin/python3 youtube_watch.py

git add youtube_seen.json
if ! git diff --cached --quiet; then
  git commit -q -m "chore: youtube_watch 更新已讀影片記錄（本機） [skip ci]"
  git pull --rebase --autostash -q && git push -q || echo "❌ git push 失敗，下次執行再推"
fi
