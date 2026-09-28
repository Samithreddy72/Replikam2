#!/bin/bash
# deploy.sh, run for real against a stand-in fleet host (2026-09-28): it must refuse to deploy a
# commit that lacks what production runs, change nothing live when the build or the proxy config
# fails, back up and recover from a crash-looping build, and roll back without a rebuild.
#
# The real script runs from a throwaway git repo (so "older", "newer" and "diverged" commits are
# real commits). Only what needs the Lightsail box is a stand-in: ssh runs the remote command
# locally with /opt/netbridge moved into a sandbox, and docker/curl are small fakes that keep the
# host's state (images, tags, the fleet container) in a JSON file. The database backup and restore
# run the script's own Python against a real SQLite file. No network, no Docker, no fleet.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$HERE/.."
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
pass=0; fail=0
ok(){ echo "  PASS  $1"; pass=$((pass+1)); }
no(){ echo "  FAIL  $1"; [ -n "${2:-}" ] && printf '%s\n' "$2" | tail -12 | sed 's/^/        /'; fail=$((fail+1)); }
check(){ if eval "$1"; then ok "$2"; else no "$2" "$(cat "$T/out" 2>/dev/null)"; fi; }

export SB="$T/host"
mkdir -p "$T/bin" "$SB/opt/netbridge" "$SB/data" "$SB/flags"
echo "FLEET_DOMAIN=fleet.test" > "$SB/opt/netbridge/.env"

# ---- stand-ins for the host ----------------------------------------------------------------
cat > "$T/bin/ssh" <<'EOF'
#!/bin/bash
while [ $# -gt 0 ]; do case "$1" in -i|-o) shift 2 ;; *) break ;; esac; done
shift                                               # ubuntu@host
cmd="$*"
cmd="${cmd//\/opt\/netbridge/$SB/opt/netbridge}"
echo "SSH $cmd" >> "$SB/ssh.log"
exec bash -c "$cmd"
EOF
cat > "$T/bin/scp" <<'EOF'
#!/bin/bash
# only the pre-2026-09-28 deploy.sh uses scp; kept so this test can prove it fails there
a=(); while [ $# -gt 0 ]; do case "$1" in -q) shift ;; -i|-o) shift 2 ;; *) a+=("$1"); shift ;; esac; done
n=${#a[@]}; dest="${a[$((n-1))]}"; dest="${dest#*:}"; dest="${dest//\/opt\/netbridge/$SB/opt/netbridge}"
unset "a[$((n-1))]"; cp "${a[@]}" "$dest"
EOF
printf '#!/bin/bash\nexec "$@"\n' > "$T/bin/sudo"
printf '#!/bin/bash\nexit 0\n' > "$T/bin/sleep"
printf '#!/bin/bash\nexit 0\n' > "$T/bin/chown"          # there is no ubuntu user here
cat > "$T/bin/docker" <<'EOF'
#!/usr/bin/env python3
import hashlib, json, os, pathlib, re, subprocess, sys, time
SB = pathlib.Path(os.environ["SB"]); F = SB / "flags"; P = SB / "docker.json"
args = sys.argv[1:]
with open(SB / "docker.log", "a") as f:
    f.write("DOCKER " + " ".join(args) + "\n")
st = json.loads(P.read_text()) if P.exists() else {"images": {}, "tags": {}, "fleet": None}
save = lambda: P.write_text(json.dumps(st))
resolve = lambda ref: ref if ref.startswith("sha256:") else st["tags"].get(ref)
flag = lambda n: (F / n).exists()                           # a stand-in fault is switched on
crash_sha = (F / "crash_sha").read_text().strip() if flag("crash_sha") else None
def env_of(img):
    print("PATH=/usr/local/bin\nCONTROL_PLANE_GIT_SHA=%s\n" % st["images"][img]["sha"])
def python(rest):                     # python - <args>: the script arrives on stdin
    mapped = [a.replace("/data/", str(SB / "data") + "/") for a in rest[2:]]
    sys.exit(subprocess.run([sys.executable, "-"] + mapped, stdin=sys.stdin).returncode)
def opts(a, with_value):
    while a and a[0].startswith("-"):
        a = a[2:] if a[0] in with_value else a[1:]
    return a
fleet = st["fleet"]
if args[0] == "compose":
    a, files = args[1:], []
    while a and a[0].startswith("-"):
        if a[0] == "-f":
            files.append(a[1])
        a = a[2:] if a[0] in ("-f", "-p") else a[1:]
    sub, a = a[0], a[1:]
    if sub == "ps":
        if "-q" in a:
            if fleet and ("--status" not in a or fleet["state"] == "running"):
                print("cid-fleet")
        else:
            print("fleet  %s  %s" % ((fleet or {}).get("state", "absent"), (fleet or {}).get("image", "")[:19]))
    elif sub == "exec":
        a = opts(a, ())
        if a[0] == "fleet":
            if not fleet or fleet["state"] != "running":
                sys.exit("Error response from daemon: Container cid-fleet is restarting, wait until the container is running")
            with open(SB / "backup-from.log", "a") as f:
                f.write("exec %s\n" % fleet["image"])
            python(a[1:])
        if a[0] == "caddy" and "validate" in a:       # how the pre-2026-09-28 script validated
            if flag("caddy_invalid"):
                sys.exit("Error: adapting config using caddyfile: Caddyfile:9: unrecognized directive: reverse_prxy")
            print("Valid configuration")
        if a[0] == "caddy" and "reload" in a:
            if flag("reload_fail"):
                sys.exit("Error: loading new config: http app module: start: listening on :443: address already in use")
            (SB / "caddy-loaded").write_text((SB / "opt/netbridge/Caddyfile").read_text())
            print("reloaded")
    elif sub == "run":
        a = opts(a, ("-v", "-e"))
        if a[0] == "fleet":
            ref = "netbridge-fleet:latest"            # the service's image, unless an override names one
            for name in files:
                if name not in ("docker-compose.yml", "docker-compose.yml.new"):
                    m = re.search(r'image:\s*"?([^"\s]+)"?', pathlib.Path(name).read_text())
                    ref = m.group(1) if m else ref
            img = resolve(ref)
            if not img:
                sys.exit("Error: pull access denied for %s" % ref)
            with open(SB / "backup-from.log", "a") as f:
                f.write("run %s\n" % img)
            python(a[1:])
        if a[0] == "caddy" and "validate" in a:
            if flag("caddy_invalid"):
                sys.exit("Error: adapting config using caddyfile: Caddyfile:9: unrecognized directive: reverse_prxy")
            print("Valid configuration")
    elif sub == "up":
        img = resolve("netbridge-fleet:latest")
        crash = st["images"][img]["sha"] == crash_sha
        st["fleet"] = {"image": img, "state": "restarting" if crash else "running"}
        save()
    elif sub == "stop":
        if fleet:
            fleet["state"] = "exited"; save()
    elif sub == "logs":
        if fleet and fleet["state"] == "restarting":
            print("fleet-1  | Traceback (most recent call last):\nfleet-1  | sqlite3.IntegrityError: FAKE BACKFILL CRASH")
        else:
            print("fleet-1  | INFO: Application startup complete.")
elif args[0] == "inspect":
    fmt = args[args.index("-f") + 1]
    if not fleet:
        sys.exit("Error: No such object")
    if "State.Status" in fmt: print(fleet["state"])
    elif ".Image" in fmt: print(fleet["image"])
    else: env_of(fleet["image"])
elif args[0] == "image" and args[1] == "inspect":
    a = args[2:]
    fmt = a[a.index("-f") + 1] if "-f" in a else "{{json .}}"
    refs = [x for i, x in enumerate(a) if not x.startswith("-") and (i == 0 or a[i - 1] != "-f")]
    missing = False
    for ref in refs:
        img = resolve(ref)
        if not img:
            print("Error: No such image: %s" % ref, file=sys.stderr); missing = True
        elif ".Id" in fmt: print(img)
        elif "Env" in fmt: env_of(img)
        else: print(json.dumps({"Id": img}))
    sys.exit(1 if missing else 0)
elif args[0] == "images":
    for tag, img in sorted(st["tags"].items()):
        if tag.startswith("netbridge-fleet:"):
            print("%s %s" % (tag, img))
elif args[0] == "rmi":
    ref = args[-1]; img = st["tags"].get(ref)
    if not img:
        sys.exit("Error: No such image: %s" % ref)
    others = [t for t, i in st["tags"].items() if i == img and t != ref]
    if not others and fleet and fleet["image"] == img:
        sys.exit("Error: conflict: unable to remove repository reference %s - container cid-fleet is using its referenced image" % ref)
    del st["tags"][ref]; save(); print("Untagged: %s" % ref)
elif args[0] == "build":
    if flag("build_fail"):
        sys.exit("ERROR: failed to solve: process \"/bin/sh -c pip install -r requirements.txt\" did not complete successfully")
    if flag("build_silent"):                  # exit 0 and no image id: what the old `| tail -1` hid
        sys.exit(0)
    sha = [x.split("=", 1)[1] for x in args if x.startswith("CONTROL_PLANE_GIT_SHA=")][0]
    tag = args[args.index("-t") + 1]
    img = "sha256:" + hashlib.sha256(("%s %s" % (sha, time.time_ns())).encode()).hexdigest()
    st["images"][img] = {"sha": sha, "panel": pathlib.Path("panel-dist/index.html").read_text()}
    st["tags"][tag] = img
    save(); print(img)
elif args[0] == "tag":
    img = resolve(args[1])
    if not img:
        sys.exit("Error: No such image: %s" % args[1])
    st["tags"][args[2]] = img; save()
EOF
cat > "$T/bin/curl" <<'EOF'
#!/usr/bin/env python3
import json, os, pathlib, shutil, sys
SB = pathlib.Path(os.environ["SB"]); P = SB / "docker.json"
a = sys.argv[1:]
out = a[a.index("-o") + 1] if "-o" in a else None
url = [x for x in a if x.startswith("http")][-1]
st = json.loads(P.read_text()) if P.exists() else {"fleet": None}
f = st.get("fleet")
up = bool(f) and f["state"] == "running"
if url.endswith("/admin/stream"):
    print("401", end=""); sys.exit(0)
if not up:
    print("curl: (22) The requested URL returned error: 502", file=sys.stderr); sys.exit(22)
img = st["images"][f["image"]]
if url.endswith("/healthz"):
    print(json.dumps({"ok": True, "git_sha": img["sha"], "api_docs_public": False}))
else:
    pathlib.Path(out).write_text(img["panel"])
EOF
chmod +x "$T"/bin/*

# ---- a repository with real history --------------------------------------------------------
W="$T/repo"
mkdir -p "$W/control-plane/deploy/aws" "$W/control-plane/backend/app" "$W/control-plane/panel-dist"
cp "$REPO/control-plane/deploy/aws/deploy.sh" "$REPO/control-plane/deploy/aws/docker-compose.yml" \
   "$REPO/control-plane/deploy/aws/Caddyfile" "$W/control-plane/deploy/aws/"
echo "FROM python:3.12-slim" > "$W/control-plane/Dockerfile"
echo "fastapi" > "$W/control-plane/backend/requirements.txt"
echo "print('fleet')" > "$W/control-plane/backend/app/main.py"
g(){ git -C "$W" -c user.name=t -c user.email=t@t "$@"; }
commit(){ echo "$2" > "$W/control-plane/$1"; g add -A; g commit -q -m "$3"; g rev-parse HEAD; }
g init -q; g checkout -q -b main
C1=$(commit panel-dist/index.html "<html>panel 1</html>" "c1 first")
C2=$(commit panel-dist/index.html "<html>panel 2</html>" "c2 remove set-peer")
g checkout -q -b side "$C1"
C3=$(commit backend/app/main.py "print('side')" "c3 an old side branch")
g checkout -q main

deploy(){ # deploy [VAR=value ...] [--rollback]
  local envs=() flag=""
  while [ $# -gt 0 ]; do case "$1" in --rollback) flag="--rollback" ;; *) envs+=("$1") ;; esac; shift; done
  : > "$SB/docker.log"; : > "$SB/backup-from.log"
  ( cd "$W" && env PATH="$T/bin:$PATH" DOMAIN=fleet.test ${envs[@]+"${envs[@]}"} \
      bash "$W/control-plane/deploy/aws/deploy.sh" $flag host.test "$T/key" ) > "$T/out" 2>&1
  echo $? > "$T/rc"
}
rc(){ cat "$T/rc"; }
live(){ python3 -c "import json;s=json.load(open('$SB/docker.json'));f=s['fleet'];print(s['images'][f['image']]['sha'] if f else '')"; }
state(){ python3 -c "import json;f=json.load(open('$SB/docker.json'))['fleet'];print(f['state'] if f else 'absent')"; }
set_state(){ python3 -c "import json;p='$SB/docker.json';s=json.load(open(p));s['fleet']['state']='$1';json.dump(s,open(p,'w'))"; }
tag_sha(){ python3 -c "import json;s=json.load(open('$SB/docker.json'));i=s['tags'].get('$1');print(s['images'][i]['sha'] if i else '')"; }
tag_id(){ python3 -c "import json;print(json.load(open('$SB/docker.json'))['tags'].get('$1',''))"; }
# every netbridge-fleet tag left on the host points at what latest or previous points at
tags_tidy(){ python3 -c "
import json; t = json.load(open('$SB/docker.json'))['tags']
keep = {t.get('netbridge-fleet:latest'), t.get('netbridge-fleet:previous')}
bad = [k for k, v in t.items() if k.startswith('netbridge-fleet:') and v not in keep]
print(' '.join(bad)); raise SystemExit(1 if bad else 0)"; }
built(){ grep -q "^DOCKER build" "$SB/docker.log"; }
went_up(){ grep -q "^DOCKER compose up" "$SB/docker.log"; }
line_of(){ grep -n -m1 -e "$1" "$SB/docker.log" | cut -d: -f1; }
sum(){ shasum -a 256 "$1" 2>/dev/null | cut -c1-16; }
inode(){ python3 -c "import os, sys; print(os.stat(sys.argv[1]).st_ino)" "$1"; }
backups(){ ls "$SB/data" | grep -c '^bridge.db.pre-[0-9a-f]'; }
seed_running(){ python3 - "$1" "$2" <<PY
import json, sys; p = "$SB/docker.json"; sha, panel = sys.argv[1], sys.argv[2]
s = json.load(open(p)) if __import__("os").path.exists(p) else {"images": {}, "tags": {}, "fleet": None}
s["images"]["sha256:seed"] = {"sha": sha, "panel": panel}
s["tags"]["netbridge-fleet:latest"] = s["tags"]["netbridge-fleet:" + sha] = "sha256:seed"
s["fleet"] = {"image": "sha256:seed", "state": "running"}; json.dump(s, open(p, "w"))
PY
cp "$W/control-plane/deploy/aws/docker-compose.yml" "$W/control-plane/deploy/aws/Caddyfile" "$SB/opt/netbridge/"; }
db(){ python3 - "$@" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
if sys.argv[2] == "init":
    c.executescript("create table devices(id text); insert into devices values ('bridge-1');"
                    "create table commands(id integer primary key, type text, status text, args text);"
                    "create table marks(v text);")
    c.execute("insert into commands(type, status, args) values ('set-pin', 'done', '{\"pin\": \"123456\"}')")
elif sys.argv[2] == "mark":
    c.execute("insert into marks values (?)", (sys.argv[3],))
else:
    print(",".join(r[0] for r in c.execute("select v from marks order by rowid")))
c.commit()
PY
}

echo "deploy.sh against a stand-in host"
echo "================================="

# ---- bring-up --------------------------------------------------------------------------------
g checkout -q "$C1"
mv "$SB/opt/netbridge/.env" "$T/env.saved"
deploy
check '[ "$(rc)" != 0 ] && grep -q ".env does not exist" "$T/out" && ! built && [ ! -f "$SB/opt/netbridge/docker-compose.yml" ]' \
      "no .env on the host: refused before anything is sent or built"
mv "$T/env.saved" "$SB/opt/netbridge/.env"

deploy
check '[ "$(rc)" = 0 ] && [ "$(live)" = "$C1" ] && grep -q "first deploy" "$T/out" && grep -q "\[deploy\] done" "$T/out"' \
      "first deploy: nothing to back up, the new build is live and verified"
check '[ -z "$(tag_id netbridge-fleet:previous)" ]' "…and nothing is recorded as previous (nothing was serving before it)"
check '[ "$(tag_sha "netbridge-fleet:$C1")" = "$C1" ] && [ "$(tag_id netbridge-fleet:latest)" = "$(tag_id "netbridge-fleet:$C1")" ]' \
      "the build is tagged with its own commit as well as latest"
check 'grep -q "on no remote-tracking branch" "$T/out"' "a commit that exists only locally is called out"
# The pre-2026-09-28 script cannot do a first deploy at all (its backup step needs a running
# container). Put C1 in place by hand then, so each check below still tests its own behaviour
# instead of failing in a cascade.
[ "$(rc)" = 0 ] || seed_running "$C1" "<html>panel 1</html>"
db "$SB/data/bridge.db" init

# ---- a normal update ---------------------------------------------------------------------------
g checkout -q "$C2"
deploy
BACKUP=$(sed -n 's#.*backing up the database -> /data/\(bridge.db.pre-[^ ]*\).*#\1#p' "$T/out")
check '[ "$(rc)" = 0 ] && [ "$(live)" = "$C2" ] && grep -q "backup ok: 1 devices; 1 finished PIN" "$T/out" && [ -f "$SB/data/$BACKUP" ]' \
      "an update backs up the database (PINs scrubbed in the copy) and goes live"
check '[ "$(tag_sha netbridge-fleet:previous)" = "$C1" ]' "…and keeps the build it replaced as netbridge-fleet:previous"
check 'grep -q "production.s $C1 is contained" "$T/out"' "…after checking that the new commit contains the live one"
B=$(line_of "^DOCKER build"); K=$(line_of "compose exec -T fleet python"); L=$(line_of "^DOCKER tag netbridge-fleet:$C2 netbridge-fleet:latest"); U=$(line_of "^DOCKER compose up")
check '[ -n "$B" ] && [ -n "$K" ] && [ -n "$L" ] && [ -n "$U" ] && [ "$B" -lt "$K" ] && [ "$K" -lt "$L" ] && [ "$L" -lt "$U" ]' \
      "…in order: build, then back up from the running app, then switch, then start"

# ---- the ancestry guard --------------------------------------------------------------------------
g checkout -q side
deploy
check '[ "$(rc)" != 0 ] && grep -q "does not contain it" "$T/out" && grep -q "c2 remove set-peer" "$T/out"' \
      "a diverged worktree is refused, naming the control-plane commits it would undo"
check '! built && [ "$(live)" = "$C2" ]' "…and nothing was built or changed"
g checkout -q "$C1"
deploy
check '[ "$(rc)" != 0 ] && grep -q "ROLLBACK=1" "$T/out" && ! built && [ "$(live)" = "$C2" ]' \
      "an OLDER commit is refused too, and the refusal says how to roll back on purpose"
cp "$SB/docker.json" "$T/docker.saved"
python3 - <<PY
import json; p = "$SB/docker.json"; s = json.load(open(p)); s["fleet"] = None; json.dump(s, open(p, "w"))
PY
deploy
check '[ "$(rc)" != 0 ] && grep -q "does not contain it" "$T/out" && ! built' \
      "…also when no fleet container exists: the commit the latest image was built from counts"
cp "$T/docker.saved" "$SB/docker.json"
deploy ROLLBACK=1
check '[ "$(rc)" = 0 ] && [ "$(live)" = "$C1" ] && grep -q "rolling back deliberately" "$T/out"' \
      "ROLLBACK=1 deploys the older commit deliberately"
g checkout -q main
deploy
check '[ "$(rc)" = 0 ] && [ "$(live)" = "$C2" ]' "forward again from the newer checkout"
cp "$SB/docker.json" "$T/docker.saved"
python3 - <<PY
import json; p = "$SB/docker.json"; s = json.load(open(p))
s["images"]["sha256:elsewhere"] = {"sha": "0123456789abcdef0123456789abcdef01234567", "panel": "x"}
s["fleet"]["image"] = "sha256:elsewhere"; json.dump(s, open(p, "w"))
PY
deploy
check '[ "$(rc)" != 0 ] && grep -q "a commit this clone does not have" "$T/out" && ! built' \
      "production on a commit this clone has never seen: refused (fetch first)"
cp "$T/docker.saved" "$SB/docker.json"
echo "local change" >> "$W/control-plane/backend/app/main.py"
SSH_BEFORE=$(wc -l < "$SB/ssh.log")
deploy
check '[ "$(rc)" != 0 ] && grep -q "uncommitted change" "$T/out" && ! built && [ "$(wc -l < "$SB/ssh.log")" = "$SSH_BEFORE" ]' \
      "a dirty tree is still refused, before anything talks to the host"
g checkout -q -- control-plane/backend/app/main.py

# ---- failures before the switch change nothing live ------------------------------------------------
C4=$(commit backend/requirements.txt "fastapi\nbroken-dependency" "c4 a bad requirement")
CADDY_BEFORE=$(sum "$SB/opt/netbridge/Caddyfile"); COMPOSE_BEFORE=$(sum "$SB/opt/netbridge/docker-compose.yml")
N_BACKUPS=$(backups)
touch "$SB/flags/build_fail"
deploy
check '[ "$(rc)" != 0 ] && grep -q "the image did not build" "$T/out" && grep -q "failed to solve" "$T/out"' \
      "a failed image build is reported as a failed build, with its error"
check '! went_up && [ "$(live)" = "$C2" ] && [ "$(sum "$SB/opt/netbridge/Caddyfile")" = "$CADDY_BEFORE" ] && [ "$(sum "$SB/opt/netbridge/docker-compose.yml")" = "$COMPOSE_BEFORE" ] && [ "$(backups)" = "$N_BACKUPS" ]' \
      "…and the running app, compose file and Caddyfile are exactly as they were"
rm -f "$SB/flags/build_fail"
touch "$SB/flags/build_silent"
deploy
check '[ "$(rc)" != 0 ] && grep -q "the image did not build" "$T/out"' \
      "a build that 'succeeds' with no image id is a failed build, not a deploy of nothing"
check '! went_up && [ "$(live)" = "$C2" ] && [ "$(sum "$SB/opt/netbridge/Caddyfile")" = "$CADDY_BEFORE" ] && [ "$(backups)" = "$N_BACKUPS" ]' \
      "…and nothing live changed"
rm -f "$SB/flags/build_silent"

printf '\n# a later proxy change\n' >> "$W/control-plane/deploy/aws/Caddyfile"; g add -A; g commit -q -m "c5 proxy change"; C5=$(g rev-parse HEAD)
touch "$SB/flags/caddy_invalid"
deploy
check '[ "$(rc)" != 0 ] && grep -q "does not validate" "$T/out" && grep -q "unrecognized directive" "$T/out"' \
      "an invalid Caddyfile fails the deploy (exit 1), with Caddy's own error"
check '! went_up && [ "$(live)" = "$C2" ] && [ "$(sum "$SB/opt/netbridge/Caddyfile")" = "$CADDY_BEFORE" ] && [ ! -e "$SB/opt/netbridge/Caddyfile.new" ]' \
      "…and the live Caddyfile was never replaced (the next restart still comes up)"
rm -f "$SB/flags/caddy_invalid"

# ---- a build that crash-loops, and the ways back ------------------------------------------------------
echo "$C5" > "$SB/flags/crash_sha"
INODE_BEFORE=$(inode "$SB/opt/netbridge/Caddyfile")
deploy
check '[ "$(rc)" != 0 ] && [ "$(state)" = restarting ] && grep -q "crashing on start" "$T/out" && grep -q "FAKE BACKFILL CRASH" "$T/out"' \
      "a build that crashes on start fails verification and shows the host's logs"
check '[ "$(tag_sha netbridge-fleet:previous)" = "$C2" ]' "…the build it replaced is recorded as previous"
check '[ "$(inode "$SB/opt/netbridge/Caddyfile")" = "$INODE_BEFORE" ] && [ "$(sum "$SB/opt/netbridge/Caddyfile")" != "$CADDY_BEFORE" ] && cmp -s "$SB/opt/netbridge/Caddyfile" "$W/control-plane/deploy/aws/Caddyfile" && cmp -s "$SB/caddy-loaded" "$W/control-plane/deploy/aws/Caddyfile"' \
      "…and the validated Caddyfile was written in place and reloaded"
db "$SB/data/bridge.db" mark during-crash
g checkout -q "$C2"
deploy ROLLBACK=1
check '[ "$(rc)" = 0 ] && [ "$(live)" = "$C2" ] && grep -q "one-off container" "$T/out" && grep -q "backup ok" "$T/out"' \
      "redeploying the previous commit during the crash loop works: the backup comes from a one-off container"
check 'grep -qx "run $(tag_id "netbridge-fleet:$C2")" "$SB/backup-from.log"' \
      "…of the image just built (the crashing one is not needed to read the database)"
check '[ "$(tag_sha netbridge-fleet:previous)" = "$C2" ] && grep -q "not serving" "$T/out"' \
      "…and the crashing build never becomes 'previous'"
check 'T0=$(tags_tidy) && grep -q "^DOCKER rmi netbridge-fleet:$C5" "$SB/docker.log"' \
      "after a verified deploy only the builds latest and previous name keep a tag (the crashing one is gone)"
g checkout -q main
deploy
check '[ "$(rc)" != 0 ] && [ "$(state)" = restarting ] && [ "$(live)" = "$C5" ]' "the same bad build again: crash loop"
deploy --rollback
check '[ "$(rc)" = 0 ] && [ "$(live)" = "$C2" ] && [ "$(state)" = running ] && grep -q "rolling back" "$T/out" && ! built' \
      "deploy.sh --rollback puts the previous build back without building anything"
deploy --rollback
check '[ "$(rc)" = 0 ] && grep -q "nothing to do" "$T/out" && ! went_up' "a second --rollback sees it is already serving and does nothing"
set_state exited
deploy --rollback
check '[ "$(rc)" = 0 ] && ! grep -q "nothing to do" "$T/out" && went_up && [ "$(state)" = running ] && [ "$(live)" = "$C2" ]' \
      "…but a stopped fleet on the previous build is started again, not reported as fine"
rm -f "$SB/flags/crash_sha"

db "$SB/data/bridge.db" mark after-backup
deploy RESTORE_DB="../../etc/passwd" --rollback
check '[ "$(rc)" != 0 ] && grep -q "RESTORE_DB must be" "$T/out" && ! went_up' "RESTORE_DB accepts only a backup file name"
deploy RESTORE_DB="bridge.db.pre-0000000-20990101000000" --rollback
check '[ "$(rc)" != 0 ] && grep -q "NOT restored" "$T/out" && grep -q "no such backup" "$T/out" && [ "$(state)" = running ] && [ "$(db "$SB/data/bridge.db" read)" = "during-crash,after-backup" ]' \
      "a backup name that does not exist: nothing restored, the fleet comes back on its own database"
deploy RESTORE_DB="$BACKUP" --rollback
SAFETY=$(ls "$SB/data" | grep pre-restore | head -1)
check '[ "$(rc)" = 0 ] && [ "$(db "$SB/data/bridge.db" read)" = "" ] && [ -n "$SAFETY" ] && [ "$(db "$SB/data/$SAFETY" read)" = "during-crash,after-backup" ]' \
      "--rollback with RESTORE_DB restores that backup and keeps the database it replaced"
check 'awk "/compose stop fleet/{s=NR} /compose run --rm --no-deps -T fleet python/{r=NR} END{exit !(s && r > s)}" "$SB/docker.log" && [ "$(state)" = running ]' \
      "…with the fleet stopped during the copy, and running again afterwards"

# ---- a Caddyfile that validates but will not load ---------------------------------------------------------
LIVE_CADDY=$(sum "$SB/opt/netbridge/Caddyfile")
printf '\n# yet another proxy change\n' >> "$W/control-plane/deploy/aws/Caddyfile"; g add -A; g commit -q -m "c6 proxy"
touch "$SB/flags/reload_fail"
deploy
check '[ "$(rc)" != 0 ] && grep -q "would not load" "$T/out" && [ "$(sum "$SB/opt/netbridge/Caddyfile")" = "$LIVE_CADDY" ]' \
      "a reload refusal fails the deploy and puts the previous Caddyfile back on disk"
rm -f "$SB/flags/reload_fail"

echo
echo "  $pass passed, $fail failed"
[ "$fail" = 0 ]
