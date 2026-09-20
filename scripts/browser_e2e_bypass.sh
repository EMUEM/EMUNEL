#!/usr/bin/env bash
# Browser E2E for the Bypass tab — boots the stack, drives a real browser
# through login -> Bypass -> profile/test/keys/generate interactions, checks
# the JS console is clean, saves screenshots, then tears down. ONE invocation
# (the sandbox reaps background processes when the shell session ends).
set -u
cd /home/z/my-project/work/EMUNEL
PORT=3783
LOG=/tmp/emunel-bypass-e2e.log
D=/home/z/my-project/download
B="http://127.0.0.1:$PORT"
mkdir -p "$D"

rm -rf .emunel-data/bypass-browser-e2e
pkill -f "emunel_core" 2>/dev/null || true
sleep 1

env -u DATABASE_URL -u EMUNEL_ENGINES_ENABLED -u EMUNEL_ENGINE_DATA \
    PORT=$PORT EMUNEL_ENGINE_DATA=$PWD/.emunel-data/bypass-browser-e2e \
    python main.py > "$LOG" 2>&1 &
SRV=$!
cleanup() { kill $SRV 2>/dev/null; pkill -f "emunel_core" 2>/dev/null; }
trap cleanup EXIT
for i in $(seq 1 40); do
  sleep 1
  [ "$(curl -s -o /dev/null -w '%{http_code}' $B/health 2>/dev/null)" = "200" ] && break
done
if [ "$(curl -s -o /dev/null -w '%{http_code}' $B/health 2>/dev/null)" != "200" ]; then
  echo "SERVER FAILED TO BOOT"; tail -20 "$LOG"; exit 1
fi
echo "SERVER UP (pid $SRV)"

agent-browser open "$B/" >/dev/null
sleep 1
# login: fill by visible input ids through a targeted selector
agent-browser eval 'document.querySelector("#u").value="admin"; document.querySelector("#p").value="admin"; document.querySelector("#go").click(); "submitted"' >/dev/null
sleep 2
after_login=$(agent-browser get url)
echo "after login url: $after_login"

# navigate to Bypass (admin nav item)
agent-browser eval 'document.querySelector("[data-nav=bypass]").click(); "nav"' >/dev/null
sleep 2
title=$(agent-browser get title)
echo "page title: $title"

# wait for the SNI card content
agent-browser wait --text "SNI Spoofing" >/dev/null 2>&1
agent-browser wait --text "REALITY" >/dev/null 2>&1
sleep 1
agent-browser screenshot "$D/emunel-bypass-page.png" >/dev/null
echo "screenshot 1 saved"

# verify the page content loaded both engine cards + stats
sni_present=$(agent-browser eval 'document.body.innerText.includes("SNI Spoofing — client-side bypass profile")')
rea_present=$(agent-browser eval 'document.body.innerText.includes("REALITY — TLS camouflage")')
runtime_honest=$(agent-browser eval 'document.body.innerText.includes("runtime — not configured")')
echo "SNI card: $sni_present | REALITY card: $rea_present | honest runtime: $runtime_honest"

# open the SNI editor and run the test
agent-browser eval 'var d=document.querySelector("#bp-sni details"); if(d)d.open=true; "opened"' >/dev/null
sleep 1
agent-browser screenshot "$D/emunel-bypass-sni-editor.png" >/dev/null
agent-browser eval 'document.querySelector("#bp-test").click(); "test"' >/dev/null
sleep 2
modal_seen=$(agent-browser eval '!!document.querySelector(".qr-c") && document.body.innerText.includes("bypass plan test")')
echo "test modal: $modal_seen"
agent-browser eval 'var x=document.querySelector("#bpx"); if(x)x.click(); "closed"' >/dev/null
sleep 1

# REALITY: generate a keypair (shows private key once in modal)
agent-browser eval 'var d=document.querySelector("#bp-rea details"); if(d)d.open=true; "opened"' >/dev/null
sleep 1
agent-browser eval 'document.querySelector("#bp-keys").click(); "keys"' >/dev/null
sleep 2
keys_modal=$(agent-browser eval '!!document.querySelector(".qr-c") && document.body.innerText.includes("private_key:")')
echo "keys modal: $keys_modal"
agent-browser screenshot "$D/emunel-bypass-keys.png" >/dev/null
agent-browser eval 'var x=document.querySelector("#bpx"); if(x)x.click(); "closed"' >/dev/null

# REALITY: generate the RAW client config
agent-browser eval 'document.querySelector("[data-gen=raw]").click(); "gen"' >/dev/null
sleep 2
gen_modal=$(agent-browser eval '!!document.querySelector(".qr-c") && document.body.innerText.includes("client outbound JSON")')
link_in_modal=$(agent-browser eval 'document.body.innerText.includes("vless://")')
echo "generate modal: $gen_modal | vless link: $link_in_modal"
agent-browser screenshot "$D/emunel-bypass-generated.png" >/dev/null
agent-browser eval 'var x=document.querySelector("#bpx"); if(x)x.click(); "closed"' >/dev/null

# helper usage modal
agent-browser eval 'document.querySelector("#bp-cmd").click(); "cmd"' >/dev/null
sleep 1
usage_modal=$(agent-browser eval '!!document.querySelector(".qr-c") && document.body.innerText.includes("emunel_sni_helper.py")')
echo "helper usage modal: $usage_modal"
agent-browser screenshot "$D/emunel-bypass-usage.png" >/dev/null
agent-browser eval 'var x=document.querySelector("#bpx"); if(x)x.click(); "closed"' >/dev/null

# JS console must be clean
errors=$(agent-browser errors)
echo "--- page errors ---"
echo "$errors"
console_lines=$(agent-browser console | rg -i "error" | rg -v "401" | head -5 || true)
echo "--- console errors (excl. expected 401s) ---"
echo "${console_lines:-NONE}"

# Engines page still intact
agent-browser eval 'document.querySelector("[data-nav=engines]").click(); "nav"' >/dev/null
sleep 2
agent-browser wait --text "Engine Settings" >/dev/null 2>&1
engines_ok=$(agent-browser eval 'document.body.innerText.includes("Engine Settings") && document.body.innerText.includes("SNISpoof")')
echo "engines page intact + SNISpoof listed: $engines_ok"
agent-browser screenshot "$D/emunel-bypass-engines-page.png" >/dev/null

echo "ALL-DONE"
