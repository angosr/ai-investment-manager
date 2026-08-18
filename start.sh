#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
trendradar_sha="8ee26026ba6c11dec41a95fb3895a7162876caa1"
newsnow_tag="v0.0.41"

mkdir -p "$script_dir/upstream" "$script_dir/data/trendradar"

if [[ ! -d "$script_dir/upstream/trendradar/.git" ]]; then
  git clone --no-tags https://github.com/sansan0/TrendRadar.git "$script_dir/upstream/trendradar"
  git -C "$script_dir/upstream/trendradar" checkout --detach "$trendradar_sha"
fi

actual_trendradar_sha="$(git -C "$script_dir/upstream/trendradar" rev-parse HEAD)"
if [[ "$actual_trendradar_sha" != "$trendradar_sha" ]]; then
  echo "TrendRadar 版本不符：期望 $trendradar_sha，实际 $actual_trendradar_sha" >&2
  exit 1
fi

if [[ ! -d "$script_dir/upstream/newsnow/.git" ]]; then
  git clone --branch "$newsnow_tag" --depth 1 https://github.com/ourongxing/newsnow.git "$script_dir/upstream/newsnow"
fi

actual_newsnow_tag="$(git -C "$script_dir/upstream/newsnow" describe --tags --exact-match 2>/dev/null || true)"
if [[ "$actual_newsnow_tag" != "$newsnow_tag" ]]; then
  echo "NewsNow 版本不符：期望 $newsnow_tag，实际 ${actual_newsnow_tag:-无标签}" >&2
  exit 1
fi

docker compose -f "$script_dir/docker-compose.yml" up -d --build
docker compose -f "$script_dir/docker-compose.yml" ps
