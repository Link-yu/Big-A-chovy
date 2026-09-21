#!/bin/bash
# keep_proxy_alive.sh — 代理健康守护（软件无关版）
#
# 2026-09-08 重写。原版问题：
#   硬编码 PROXY_PORT=7897（Clash Verge）+ `open -a "Clash Verge"`，导致
#   ① 与新代理软件(Clash Party, 7890)争夺 macOS 系统代理，每 30s 互相覆盖；
#   ② 关掉 Clash Verge ⇒ 7897 无监听 ⇒ 系统代理指向死端口 ⇒ 全网断；
#   ③ Verge 被自动拉起，关不掉。
#   实测 Verge 出口下 82.push2.eastmoney.com（资金流接口）直接超时，危害大于收益。
#
# 新行为（软件无关）：
#   1. 按 CANDIDATE_PORTS 顺序探测，选出第一个"活着且能代理行情"的端口
#   2. 系统代理指向它；若已经是它则不做任何改动
#   3. 没有可用候选（全部挂了，或候选列表为空）→ 关闭系统代理，回退直连
#      （2026-09-08 实测行情接口直连全通；2026-09-21 起 candidate_ports 即为空）
#
# 候选列表只放「本机常驻 + 国内出口」的端口：海外出口（如 Clash 日本节点）会让东财
# push2/push2his 大面积失败；WorkBuddy 沙箱的 sandbox-cli 端口也非本机常驻服务。
#   4. 绝不自动启动任何代理 App —— 换软件只需改下面的端口顺序
#
# 由 ~/Library/LaunchAgents/com.luqiang.keepclashproxy.plist 每 30 秒调用一次。

# ---- 配置 ----
# 候选代理端口，按优先级排列。换代理软件时改 proxy_ports.json 即可
# （与 scripts/network_path.py 共用同一份配置，避免两处不同步）。
#   空数组 [] = 明确禁用全部代理端口，脚本会关闭系统代理回退直连。
#   2026-09-21：原实现在候选为空时回落到硬编码 "7890 7897"，把「禁用代理」
#   静默翻译成「启用代理」——与 network_path.load_candidate_ports 的旧 bug 同类。
#   当前配置即为 []（本机直连实测可用，代理是纯负资产），故该回落已移除。
ports=""
PORTS_CONFIG="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/proxy_ports.json"
if [ -f "$PORTS_CONFIG" ]; then
    # 从 json 里抠出 candidate_ports 数组内容（不引入 jq/python 依赖）
    cfg=$(sed -n 's/.*"candidate_ports"[[:space:]]*:[[:space:]]*\[\([^]]*\)\].*/\1/p' "$PORTS_CONFIG" \
          | tr -d ' "' | tr ',' ' ' | tr -s ' ')
    # 只保留纯数字项，防 json 写坏时脚本失控
    ports=""
    for p in $cfg; do
        case "$p" in
            ''|*[!0-9]*) continue ;;
            *) ports="$ports $p" ;;
        esac
    done
fi
# ports 为空即代表「无候选代理」：不回落任何默认端口，走直连。
read -r -a CANDIDATE_PORTS <<< "${ports// }"
PROXY_HOST="127.0.0.1"
# 健康检查：能通过这个 URL 拿到 HTTP 200 才算代理可用（东财实时行情，全天可访问）
HEALTH_URL="https://push2delay.eastmoney.com/api/qt/clist/get?pn=1&pz=1&fs=m:1+t:2"
HEALTH_TIMEOUT=5
LOG="$HOME/Library/Logs/keep_proxy.log"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $1" >> "$LOG"
}

mkdir -p "$(dirname "$LOG")"

# ---- 1. 探测第一个可用的代理端口 ----
alive_port=""
for port in "${CANDIDATE_PORTS[@]}"; do
    # 先看端口有没有人监听，避免对死端口空等 HTTP 超时
    if ! nc -z -w 2 "$PROXY_HOST" "$port" 2>/dev/null; then
        continue
    fi
    code=$(env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY \
           curl -s -o /dev/null -w "%{http_code}" \
           -x "http://$PROXY_HOST:$port" --max-time "$HEALTH_TIMEOUT" \
           -A "Mozilla/5.0" "$HEALTH_URL" 2>/dev/null)
    if [ "$code" = "200" ]; then
        alive_port="$port"
        break
    fi
    log "端口 $port 有监听但行情不通(HTTP $code)，跳过"
done

# ---- 2. 读取当前系统代理状态 ----
proxy_dump=$(scutil --proxy 2>/dev/null)
cur_host=$(echo "$proxy_dump" | grep "HTTPProxy" | sed -n 's/.*: \([0-9.]*\).*/\1/p' | head -1)
cur_port=$(echo "$proxy_dump" | grep "HTTPPort" | sed -n 's/.*: \([0-9]*\).*/\1/p' | head -1)
cur_enable=$(echo "$proxy_dump" | grep "HTTPEnable" | sed -n 's/.*: \([01]\).*/\1/p' | head -1)
cur_https_enable=$(echo "$proxy_dump" | grep "HTTPSEnable" | sed -n 's/.*: \([01]\).*/\1/p' | head -1)

set_all_services() {
    # $1 = on / off；on 时用全局 PROXY_HOST / alive_port
    local action="$1"
    while IFS= read -r svc; do
        [ -z "$svc" ] && continue
        case "$svc" in
            \*|*\**) continue ;;   # 跳过禁用服务
        esac
        if [ "$action" = "off" ]; then
            networksetup -setwebproxystate "$svc" off >/dev/null 2>&1
            networksetup -setsecurewebproxystate "$svc" off >/dev/null 2>&1
        else
            networksetup -setwebproxy "$svc" "$PROXY_HOST" "$alive_port" >/dev/null 2>&1
            networksetup -setsecurewebproxy "$svc" "$PROXY_HOST" "$alive_port" >/dev/null 2>&1
            networksetup -setwebproxystate "$svc" on >/dev/null 2>&1
            networksetup -setsecurewebproxystate "$svc" on >/dev/null 2>&1
        fi
    done < <(networksetup -listallnetworkservices 2>/dev/null | grep -v "^\*" | grep -v "denotes")
}

# ---- 3. 决策 ----
if [ -n "$alive_port" ]; then
    if [ "$cur_enable" = "1" ] && [ "$cur_https_enable" = "1" ] \
       && [ "$cur_host" = "$PROXY_HOST" ] && [ "$cur_port" = "$alive_port" ]; then
        :   # 已经是正确的配置，什么都不做
    else
        log "系统代理为 ${cur_host}:${cur_port}(enable=$cur_enable)，切换到 ${PROXY_HOST}:${alive_port}"
        set_all_services on
        log "系统代理已设为: ${PROXY_HOST}:${alive_port}"
    fi
else
    if [ "$cur_enable" != "1" ] && [ "$cur_https_enable" != "1" ]; then
        :   # 已经是直连状态，什么都不做
    else
        log "无可用代理(候选: ${CANDIDATE_PORTS[*]})，关闭系统代理回退直连（行情接口直连可用）"
        set_all_services off
        log "系统代理已关闭，回退直连"
    fi
fi

# ---- 4. 心跳：配置正确时脚本是静默的，定期留痕以便确认守护仍在跑 ----
# 每 30 分钟一行，避免日志被 30s 一次的空转刷爆。
HEARTBEAT_FILE="/tmp/.keep_proxy_heartbeat"
HEARTBEAT_INTERVAL=1800
now=$(date +%s)
last=0
if [ -f "$HEARTBEAT_FILE" ]; then
    last=$(cat "$HEARTBEAT_FILE" 2>/dev/null || echo 0)
fi
if [ $((now - last)) -ge "$HEARTBEAT_INTERVAL" ]; then
    echo "$now" > "$HEARTBEAT_FILE"
    final_host=$(scutil --proxy 2>/dev/null | grep "HTTPProxy" | sed -n 's/.*: \([0-9.]*\).*/\1/p' | head -1)
    final_port=$(scutil --proxy 2>/dev/null | grep "HTTPPort" | sed -n 's/.*: \([0-9]*\).*/\1/p' | head -1)
    log "心跳 守护运行中 | 可用端口=${alive_port:-无} | 系统代理=${final_host:-未设置}:${final_port:-未设置} | 候选=${CANDIDATE_PORTS[*]}"
fi

exit 0
