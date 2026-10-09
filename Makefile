# WILL-E — run these on the Mac, inside the willie/ folder.
# Override on the command line if needed:  make deploy PI=woetner@willie.local
PI      ?= willie.local
SSH     ?= ssh
PI_DIR  ?= willie
# ESP32 board for the firmware (D18): the 30-pin ESP32-WROOM DevKit (esp32dev).
MCU     ?= esp32dev
# Run a command on the Pi with the mic to itself: pause the hands-free voice service,
# and start it again afterwards - also after Ctrl-C.
MIC = sudo systemctl stop willie-voice 2>/dev/null; trap "sudo systemctl start willie-voice 2>/dev/null" EXIT;
RSYNC   := rsync -az --delete \
             --exclude .git/ --exclude .env --exclude .venv/ --exclude __pycache__/ \
             --exclude '*.log' --exclude .DS_Store --exclude firmware/.pio/ --exclude .local/

.PHONY: help sync setup diet deps service deploy restart stop logs status ssh ram link-test ask-camera face face-install voice \
        pull-config run-local fw flash flash-link monitor bench-mcu floor-cal nav-cal tof-watch roam floor-rec-pull floor-rec-clear bench-camera bench-screen bench-audio-out voice-pi live-talk voices voice-samples wake-record wake-fetch voice-logs wake-test bench-aec bench-doa mic-tune mic-tune-undo cascade-test voice-adapter voice-enroll wake-record-mac wake-label wake-eval wake-mine wake-train wake-install voice-record voice-fetch voice-bench-setup voice-label voice-bench garage improve improve-watch improve-install spotify-setup spotify-login spotify-logs radio-setup

help:
	@echo "Pi"
	@echo "  make setup        fresh Pi: copy repo + run tools/pi_setup.sh (A4)"
	@echo "  make diet         OS diet: Bluetooth/MQTT/pigpio off, journald in RAM, service limits (A8)"
	@echo "  make deploy       sync code + restart WILL-E (A5)"
	@echo "  make ram          RAM table from the Pi (A8, D21)"
	@echo "  make presence-deploy    upload silent presence code (preserves live settings)"
	@echo "  make presence-bluetooth-setup  prepare BLE without changing the MCU's PL011 UART; reboot required"
	@echo "  make presence-activate  activate prepared BLE config with a guarded idle reboot"
	@echo "  make ask-camera   take one photo and ask Gemini about it (bench prototype)"
	@echo "  make face         say 'Hey Willie' and talk; camera is automatic (bench prototype)"
	@echo "  make face-install install the short 'willie' face-console command on the Pi"
	@echo "  make voice        talk to WILL-E with the MacBook mic + speakers (temporary bridge)"
	@echo "  make link-test    200 pings Pi -> MCU, prints round-trip times (A9)"
	@echo "  make logs         follow the core log"
	@echo "  make pull-config  copy the live settings from the Pi into config/willie.yaml"
	@echo "  make bench-camera B8 camera test, photos land in ../photos/bench/b8/"
	@echo "  make bench-screen B5 screen blink fps + touch test"
	@echo "  make bench-audio-out B6 speaker test (answer the listening questions)"
	@echo "  make voice-pi     hands-free loop in the foreground (the willie-voice service does this at boot)"
	@echo "  make voice-logs   follow what the hands-free service hears and does"
	@echo "  make wake-test    2 min wake-word bench (S=600 for longer), service paused meanwhile"
	@echo "  make bench-doa    G4 sound direction: live angle, or A='0 45 -45' to test positions"
	@echo "  make mic-tune     guided mic tuning: noise, gain, sound-direction fit, 10-try check (writes settings after asking)"
	@echo "  make mic-tune-undo  restore the settings from before the last mic-tune"
	@echo "  make live-talk    spoken session until Ctrl-C, no wake word (S=60 for a timed one)"
	@echo "  make voice-adapter A=cascade   who he talks with: cascade, gemini_live or openai_realtime (restarts the voice)"
	@echo "  make cascade-test  the G1 questions on the cascade adapter: reply time, tools, answers (Q='tijd rekenen' for a few)"
	@echo "  make voice-samples make the candidate voice samples on the Pi (V='Orus Schedar' for only those)"
	@echo "  make voices       play the voice samples one after another on the Mac"
	@echo "  make wake-record  record 'Hey Willie' + everyday sound through the robot's mic (D2 round 2)"
	@echo "  make wake-fetch   copy those recordings to the Mac training workspace"
	@echo "  make wake-record Q=1 [NEG=10] [TEST=1]  quick 3-min round (T2); TEST=1 = for the never-trained test set"
	@echo "  make wake-record-mac  extra 'Hey Willie' clips at the MacBook mic (low weight)"
	@echo "  make wake-train   features, train, mine hard negatives, train again, score (Mac, 1-3 h)"
	@echo "  make wake-eval    score the wake model: heard %% and false wakes/hour per cutoff"
	@echo "  make wake-label   listen to his kept wakes and near misses: you or not?"
	@echo "  make voice-record guided test clips through the robot mic (T0b); then voice-fetch, voice-bench"
	@echo "  make voice-bench  score speech detector and speech-to-text on those clips (first: voice-bench-setup)"
	@echo "Garage mode (K1)"
	@echo "  make voice-enroll  teach the home server your voice (NEW=1 starts over); do it in the garage"
	@echo "  make garage ON=1   garage mode on (ON=0 off) - or say 'garagemodus aan'"
	@echo "Spotify"
	@echo "  make spotify-setup  install librespot: WILL-E becomes a Spotify speaker"
	@echo "  make spotify-login  link the Web API (asks for the app id/secret on the Pi, opens nothing)"
	@echo "  make spotify-logs   follow the Spotify receiver log"
	@echo "Radio"
	@echo "  make radio-setup    install mpv: live internet radio on his speaker"
	@echo "Self-improvement (runs Claude Code on the Mac, never deploys)"
	@echo "  make improve       do the changes WILL-E was asked for, on a branch"
	@echo "  make improve-watch keep watching for spoken requests"
	@echo "  make improve-install  run the watcher in the background at login (launchd)"
	@echo "MCU (needs PlatformIO on the Mac: brew install platformio)"
	@echo "  make fw           build the firmware            (MCU=$(MCU))"
	@echo "  make flash        build + flash over USB        (MCU=$(MCU))"
	@echo "  make flash-link   build + flash over the Pi link, no USB"
	@echo "  make monitor      USB serial monitor (debug output)"
	@echo "  make bench-mcu T=servo|sensors|io|motors|spin|watch   B9-B12 tests over the link"
	@echo "  make roam         F6 test: drive around for 10 min without a pause (MIN=3 for shorter), around obstacles"
	@echo "  make tof-watch    show the front ToF readings live for 30 s (hold a hand in front of each)"
	@echo "  make nav-cal      calibrate the wall heading fix: stand him square to a wall first (M4); D=reset forgets"
	@echo "  make floor-cal D=500   calibrate the camera floor scan: box D mm in front of him (F7); D=reset forgets"
	@echo "  make floor-rec-pull    fetch the floor-scan recording (dashboard: floorscan.record) to .local/floorrec"
	@echo "  make face-demo    on the Pi: 60 s face-only demo + RAM/render measurements"
	@echo "Mac"
	@echo "  make face-preview animated face studio -> http://127.0.0.1:8765"
	@echo "  make face-test    offline face + voice regression checks"
	@echo "  make run-local    core + dashboard + fake MCU on the Mac -> http://localhost:8080"

# ---------------------------------------------------------------- Pi
# The master plan lives next to the repo; copy it along so WILL-E can read it (lees_code).
sync:
	$(RSYNC) ./ ../WILL-E.md $(PI):$(PI_DIR)/

setup: sync
	ssh -t $(PI) 'bash $(PI_DIR)/tools/pi_setup.sh'

diet: sync
	ssh -t $(PI) 'bash $(PI_DIR)/tools/os_diet.sh'

deps: sync
	ssh $(PI) 'cd $(PI_DIR) && .venv/bin/pip install -q -r requirements.txt'

service: sync
	ssh $(PI) 'sudo bash $(PI_DIR)/tools/install_service.sh'

# requirements.txt changed?  ->  make deps  (deploy does not reinstall packages, to stay fast)
deploy: sync
	ssh $(PI) 'sudo systemctl restart willie willie-voice; sudo systemctl stop willie-dashboard || true'
	@echo "deployed -> http://$(PI):8080 (dashboard starts on the first visit)"

# Opt-in software update with a code snapshot. Keep the Pi's calibrated settings.
.PHONY: map3d-deploy
map3d-deploy:
	ssh $(PI) 'umask 077; mkdir -p "$$HOME/map3d-backups" && tar --exclude=.git --exclude=.env --exclude="*.env" --exclude=.venv --exclude=.local --exclude=.pio --exclude=__pycache__ --exclude=./config/willie.yaml -czf "$$HOME/map3d-backups/willie-$$(date -u +%Y%m%dT%H%M%SZ).tgz" -C $(PI_DIR) .'
	$(RSYNC) --exclude config/willie.yaml ./ ../WILL-E.md $(PI):$(PI_DIR)/
	ssh $(PI) 'cd $(PI_DIR) && .venv/bin/python -m compileall -q willie && sudo systemctl restart willie willie-voice && sudo systemctl try-restart willie-dashboard'

.PHONY: presence-deploy presence-bluetooth-setup presence-activate presence-check
presence-deploy:
	$(SSH) $(PI) 'umask 077; mkdir -p "$$HOME/presence-backups" && tar --exclude=.git --exclude=.env --exclude="*.env" --exclude=.venv --exclude=.local --exclude=.pio --exclude=__pycache__ -czf "$$HOME/presence-backups/willie-$$(date -u +%Y%m%dT%H%M%SZ).tgz" -C $(PI_DIR) .'
	rsync -az -e "$(SSH)" willie/presence.py willie/remote.py $(PI):$(PI_DIR)/willie/
	rsync -az -e "$(SSH)" willie/skills/aanwezigheid.py $(PI):$(PI_DIR)/willie/skills/
	rsync -az -e "$(SSH)" willie/voice/gemini_live.py $(PI):$(PI_DIR)/willie/voice/
	rsync -az -e "$(SSH)" config/schema.yaml $(PI):$(PI_DIR)/config/
	rsync -az -e "$(SSH)" requirements.txt $(PI):$(PI_DIR)/
	rsync -az -e "$(SSH)" tools/presence_bluetooth.py tools/os_diet.sh tools/service_user.sh $(PI):$(PI_DIR)/tools/
	$(SSH) $(PI) 'cd $(PI_DIR) && .venv/bin/pip install -q "bleak>=1.0" && .venv/bin/python -m compileall -q willie && sudo systemctl restart willie-voice && sudo systemctl stop willie-dashboard'
	@echo "Presence code deployed; existing live settings kept. First BLE setup needs presence-bluetooth-setup and presence-activate."

presence-bluetooth-setup:
	rsync -az -e "$(SSH)" tools/presence_bluetooth.py $(PI):$(PI_DIR)/tools/
	$(SSH) -t $(PI) 'cd $(PI_DIR) && sudo apt-get -y install bluez && sudo usermod -aG bluetooth willie && sudo .venv/bin/python tools/presence_bluetooth.py --apply && sudo systemctl enable bluetooth && if systemctl cat hciuart.service >/dev/null 2>&1; then sudo systemctl enable hciuart; fi'
	@echo "Boot config prepared. Run make presence-activate while idle; then presence-check, link-test and ram."

presence-check:
	$(SSH) $(PI) 'test "$$(readlink -f /dev/serial0)" = /dev/ttyAMA0 && systemctl is-active willie willie-voice bluetooth && bluetoothctl list | grep -q "^Controller " && bluetoothctl show && cd $(PI_DIR) && .venv/bin/python -c "from willie.config import Config; c=Config(); print({k:c.get(k) for k in (\"presence.enabled\", \"presence.bluetooth\", \"presence.camera\")})"'

presence-activate:
	rsync -az -e "$(SSH)" tools/presence_bluetooth.py $(PI):$(PI_DIR)/tools/
	$(SSH) $(PI) 'cd $(PI_DIR) && sudo .venv/bin/python tools/presence_bluetooth.py --activate'

restart:
	ssh $(PI) 'sudo systemctl restart willie'

stop:
	ssh $(PI) 'sudo systemctl stop willie'

logs:
	ssh -t $(PI) 'journalctl -u willie -f -n 50 -o cat'

status:
	ssh $(PI) 'systemctl status willie willie-dashboard.socket --no-pager'

ssh:
	ssh $(PI)

ram:
	ssh $(PI) 'bash $(PI_DIR)/tools/ram.sh'

# An on-demand bench tool: preserves the Pi-only .env (and its API key) during sync.
# It prompts for the question on the Pi's attached keyboard.
ask-camera: sync
	ssh -t $(PI) 'cd $(PI_DIR) && .venv/bin/python tools/ask_camera.py'

# On-demand physical UI: Pi mic + speaker + face, with a cloud-routed temporary
# wake detector. The final local wake detector is D2 (on the Pi, D19).
face: sync
	ssh -t $(PI) 'cd $(PI_DIR) && .venv/bin/python tools/willie_console.py'

face-install: sync
	ssh -t $(PI) 'bash $(PI_DIR)/tools/install_face_console.sh'

# stops the core for a moment so the test owns the serial port
link-test:
	ssh $(PI) 'sudo systemctl stop willie; cd $(PI_DIR) && .venv/bin/python -m willie.hal.link -n 200; sudo systemctl start willie'

pull-config:
	scp $(PI):.config/willie/willie.yaml config/willie.yaml
	@echo "config/willie.yaml updated — review with git diff, then commit"

# ---------------------------------------------------------------- Phase B bench tests
# The core is stopped during a test so nothing else owns the camera/screen.
bench-camera: sync
	ssh -t $(PI) 'sudo systemctl stop willie; bash $(PI_DIR)/tools/bench/camera.sh; sudo systemctl start willie'
	mkdir -p ../photos/bench/b8
	scp '$(PI):bench/b8/*.jpg' ../photos/bench/b8/

bench-screen: sync
	ssh -t $(PI) 'sudo systemctl stop willie; cd $(PI_DIR) && sudo .venv/bin/python tools/bench/screen.py; sudo systemctl start willie'

# Hands-free voice loop. Run from the Pi's console so it owns the microphone.
voice-pi: sync
	ssh -t $(PI) '$(MIC) cd $(PI_DIR) && .venv/bin/python tools/willie_voice.py'

# ---------------------------------------------------------------- Spotify (skills/spotify.py)
spotify-setup: sync
	ssh -t $(PI) 'sudo bash $(PI_DIR)/tools/install_spotify.sh'

# ssh forwards the login page's answer (127.0.0.1:8888) from the Mac's browser to the Pi.
spotify-login: sync
	ssh -t -L 8888:127.0.0.1:8888 $(PI) 'cd $(PI_DIR) && .venv/bin/python tools/spotify_login.py; sudo systemctl restart willie-voice'

spotify-logs:
	ssh -t $(PI) 'journalctl -u willie-spotify -f -n 30 -o cat'

# ---------------------------------------------------------------- Radio (skills/radio.py)
radio-setup: sync
	ssh -t $(PI) 'sudo bash $(PI_DIR)/tools/install_radio.sh'

voice-logs:
	ssh -t $(PI) 'journalctl -u willie-voice -f -n 30 -o cat'

# Adapter C (willie/voice/cascade.py): the G1 questions through speech-to-text + text model + speech.
# Q="tijd rekenen" = only those; M=openai:gpt-4.1-mini = another brain for this run. Costs a few cents.
cascade-test: sync
	ssh -t $(PI) 'cd $(PI_DIR) && .venv/bin/python tools/g1/run.py --cascade $(if $(M),--model $(M)) $(Q)'

# Which adapter he talks with: make voice-adapter A=cascade (or gemini_live, openai_realtime).
# Writes the Pi's live settings like the dashboard does, then restarts the voice service.
voice-adapter:
	ssh $(PI) 'cd $(PI_DIR) && .venv/bin/python -c "from willie.config import Config; print(\"voice.adapter =\", Config().update(dict(voice=dict(adapter=\"$(A)\")))[\"voice\"][\"adapter\"])" && sudo systemctl restart willie willie-voice'

wake-test: sync
	ssh -t $(PI) '$(MIC) cd $(PI_DIR) && .venv/bin/python tools/bench/wake.py $(or $(S),120)'

bench-aec: sync
	ssh -t $(PI) '$(MIC) cd $(PI_DIR) && .venv/bin/python tools/bench/aec_delay.py'

bench-doa: sync
	ssh -t $(PI) '$(MIC) cd $(PI_DIR) && .venv/bin/python tools/bench/doa.py $(FLIP) $(A)'

mic-tune: sync
	ssh -t $(PI) '$(MIC) cd $(PI_DIR) && .venv/bin/python tools/bench/mic_tune.py'

mic-tune-undo: sync
	ssh -t $(PI) 'cd $(PI_DIR) && .venv/bin/python tools/bench/mic_tune.py undo'

live-talk: sync
	ssh -t $(PI) '$(MIC) cd $(PI_DIR) && .venv/bin/python tools/live_talk.py $(S)'

voice-enroll: sync
	ssh -t $(PI) '$(MIC) cd $(PI_DIR) && NEW=$(NEW) .venv/bin/python tools/voice_enroll.py'

garage:
	ssh $(PI) 'cd $(PI_DIR) && .venv/bin/python -c "from willie.voice import garage; garage.set_enabled($(if $(filter 0,$(ON)),False,True)); print(\"garage mode\", garage.enabled())"'

# Wake word round 2 (D2): record Wouter through the robot's mic, fetch for training on the Mac.
wake-record: sync
	ssh -t $(PI) '$(MIC) cd $(PI_DIR) && Q=$(Q) NEG=$(NEG) TEST=$(TEST) .venv/bin/python tools/wakeword/record.py'

# Q=1 = a quick 3-minute round, NEG=10 = then 10 min of room sound, TEST=1 = for the test set (T2).
wake-fetch:
	mkdir -p .local/wakeword/samples/wouter .local/wakeword/samples/wouter_neg_long .local/wakeword/samples/test_pos .local/wakeword/samples/test_neg_long
	rsync -a $(PI):$(PI_DIR)/.local/wakeword_rec/positive/ .local/wakeword/samples/wouter/
	rsync -a $(PI):$(PI_DIR)/.local/wakeword_rec/negative/ .local/wakeword/samples/wouter_neg_long/
	-rsync -a $(PI):$(PI_DIR)/.local/wakeword_rec/test/positive/ .local/wakeword/samples/test_pos/
	-rsync -a $(PI):$(PI_DIR)/.local/wakeword_rec/test/negative/ .local/wakeword/samples/test_neg_long/
	@echo "$$(ls .local/wakeword/samples/wouter | wc -l) positives, $$(ls .local/wakeword/samples/wouter_neg_long | wc -l) long negatives; test set: $$(ls .local/wakeword/samples/test_pos | wc -l) positives, $$(ls .local/wakeword/samples/test_neg_long | wc -l) long negatives"

# Wake word v2 (T2), all on the Mac. WAKE_PY = the training venv (tools/wakeword/README.md).
WAKE_PY = .local/wakeword/.venv/bin/python
wake-record-mac:
	.venv/bin/python tools/wakeword/record_mac.py $(N)
wake-label:
	.venv/bin/python tools/wakeword/evaluate.py --label
wake-eval:
	.venv/bin/python tools/wakeword/evaluate.py $(A)
wake-mine:
	.venv/bin/python tools/wakeword/evaluate.py --mine
# Features -> train -> mine what still comes close -> train on that too -> score.
# The result is a candidate (.local/wakeword/candidate); wake-install makes it the robot's model.
ROUND := $(shell date +%m%d-%H%M)
wake-train:
	$(WAKE_PY) tools/wakeword/features.py
	$(WAKE_PY) tools/wakeword/train.py "$(STEPS)" $(ROUND)a
	.venv/bin/python tools/wakeword/evaluate.py --mine
	$(WAKE_PY) tools/wakeword/features.py
	$(WAKE_PY) tools/wakeword/train.py "$(STEPS)" $(ROUND)b
	.venv/bin/python tools/wakeword/evaluate.py --robot
	.venv/bin/python tools/wakeword/evaluate.py
wake-install:
	.venv/bin/python tools/wakeword/evaluate.py --install

# T0b: labelled test clips through the robot's mic, the clips he kept himself, and the bench on the Mac.
VOICE_PY = .venv/bin/python
voice-record: sync
	ssh -t $(PI) '$(MIC) cd $(PI_DIR) && .venv/bin/python tools/bench/voice_record.py $(N)'
voice-fetch:
	mkdir -p .local/voice/clips
	rsync -a $(PI):.local/share/willie/clips/ .local/voice/clips/
	@for kind in labelled utterance wake near; do echo "$$kind: $$(ls .local/voice/clips/$$kind 2>/dev/null | grep -c wav)"; done
voice-bench-setup:
	mkdir -p .local/voice/models/vosk
	$(VOICE_PY) -m pip install -q webrtcvad-wheels faster-whisper vosk $(if $(PARAKEET),sherpa-onnx)
	test -d .local/voice/models/vosk/vosk-model-small-nl-0.22 || (cd .local/voice/models/vosk && curl -sLO https://alphacephei.com/vosk/models/vosk-model-small-nl-0.22.zip && unzip -q vosk-model-small-nl-0.22.zip && rm vosk-model-small-nl-0.22.zip)
	$(if $(PARAKEET),test -f .local/voice/models/parakeet/tokens.txt || (mkdir -p .local/voice/models/parakeet && cd .local/voice/models/parakeet && curl -L https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8.tar.bz2 | tar -xj --strip-components 1))
voice-label:
	$(VOICE_PY) tools/bench/voice_bench.py --label
voice-bench:
	$(VOICE_PY) tools/bench/voice_bench.py $(A)

# Phase T: the wake clips the Talk tab collected on the home server (real wakes + marked false wakes).
wake-fetch-talk:
	mkdir -p .local/wakeword/samples/talk_pos .local/wakeword/samples/talk_neg
	rsync -a --delete homeserver:homeserver-data/talk/train/positive/ .local/wakeword/samples/talk_pos/
	rsync -a --delete homeserver:homeserver-data/talk/train/negative/ .local/wakeword/samples/talk_neg/
	rm -rf .local/wakeword/features/talk_pos .local/wakeword/features/talk_neg
	@echo "$$(ls .local/wakeword/samples/talk_pos | wc -l) real wakes, $$(ls .local/wakeword/samples/talk_neg | wc -l) false wakes"

# Voice audition (D7): samples are made on the Pi (API key) and played on the Mac.
voice-samples: sync
	ssh $(PI) 'cd $(PI_DIR) && .venv/bin/python tools/voice_samples.py $(V)'

voices:
	mkdir -p .local/voices && rm -f .local/voices/*.wav
	rsync -a $(PI):$(PI_DIR)/.local/voices/ .local/voices/
	@for f in $$(ls .local/voices/*.wav | sort -t/ -k3 -n); do \
	  n=$$(basename $$f .wav); echo "> $${n#*_}"; afplay $$f; sleep 1; done

bench-audio-out: sync
	ssh -t $(PI) 'sudo systemctl stop willie; cd $(PI_DIR) && .venv/bin/python tools/bench/audio_out.py; sudo systemctl start willie'

# ---------------------------------------------------------------- MCU
fw:
	cd firmware && pio run -e $(MCU)

# B9–B12 over the link, e.g. make bench-mcu T=servo  (servo | sensors | io | motors | spin | watch)
T ?= watch
bench-mcu: sync
	ssh -t $(PI) 'sudo systemctl stop willie; cd $(PI_DIR) && .venv/bin/python tools/bench/mcu.py $(T); sudo systemctl start willie'

# F7: a box D mm in front of his body -> pitch trim (1 distance), camera height (2), lens-to-front (3+)
D ?=
tof-watch: sync
	ssh -t $(PI) 'cd $(PI_DIR) && .venv/bin/python tools/tof_watch.py'

# F6: drive around without a pause for MIN minutes, around obstacles; ends on `stop`
MIN ?= 10
roam: sync
	ssh $(PI) 'cd $(PI_DIR) && .venv/bin/python -u tools/roam.py $(MIN)'

nav-cal: sync
	ssh -t $(PI) 'cd $(PI_DIR) && .venv/bin/python tools/nav_cal.py $(D)'

floor-cal: sync
	ssh -t $(PI) 'cd $(PI_DIR) && .venv/bin/python tools/floor_cal.py $(D)'

# F7 recording (floorscan.record): fetch the pictures + logs to the Mac, or empty the Pi's RAM folder
floor-rec-pull:
	mkdir -p .local/floorrec
	rsync -az $(PI):/dev/shm/willie-rec/ .local/floorrec/
	@echo "pictures: $$(ls .local/floorrec/*.jpg 2>/dev/null | wc -l)  ->  .local/floorrec/"

floor-rec-clear:
	ssh $(PI) 'rm -rf /dev/shm/willie-rec'

flash:
	cd firmware && pio run -e $(MCU) -t upload

# No USB: build on the Mac, copy to the Pi, flash over the link (firmware 0.4.0+ already on the ESP32)
flash-link: sync
	cd firmware && pio run -e $(MCU)
	scp firmware/.pio/build/$(MCU)/firmware.bin $(PI):/tmp/willie-fw.bin
	ssh -t $(PI) 'sudo systemctl stop willie; cd $(PI_DIR) && .venv/bin/python tools/mcu_ota.py /tmp/willie-fw.bin; sudo systemctl start willie'

monitor:
	cd firmware && pio device monitor -e $(MCU)

# Picks up what WILL-E was asked to change and lets Claude Code do it in a
# worktree on a branch. Deploying stays a human decision.
improve:
	bash tools/improve_worker.sh

improve-watch:
	bash tools/improve_worker.sh --watch

improve-install:
	bash tools/install_improve_worker.sh

# ---------------------------------------------------------------- Mac
# Temporary bench bridge: the Mac records and speaks, the Pi keeps the camera and
# the API key. Not the robot's voice path - that is the Gate G1 adapter (D9).
voice: sync
	test -d .venv || python3 -m venv .venv
	.venv/bin/python -c 'import sounddevice' 2>/dev/null || .venv/bin/pip install -q -r requirements-mac.txt
	.venv/bin/python tools/voice_mac.py

run-local:
	test -d .venv || python3 -m venv .venv
	.venv/bin/pip install -q -r requirements.txt
	mkdir -p .local
	test -f .local/willie.yaml || sed 's#/dev/serial0#.local/mcu#' config/willie.yaml > .local/willie.yaml
	trap 'kill $$(jobs -p) 2>/dev/null' EXIT INT TERM; \
	.venv/bin/python tools/fake_mcu.py .local/mcu & \
	sleep 1; \
	WILLIE_CONFIG=.local/willie.yaml WILLIE_DATA=.local .venv/bin/python -m willie & \
	WILLIE_CONFIG=.local/willie.yaml WILLIE_DATA=.local .venv/bin/python -m willie.dashboard --port 8080

# D6: offline preview and face-only hardware demo. Neither opens mic/camera.
.PHONY: face-preview face-demo face-test
face-preview:
	python3 tools/face_preview.py

face-demo:
	.venv/bin/python tools/bench/face.py --seconds 60

face-test:
	.venv/bin/python -m pytest tests/test_face.py tests/test_voice_adapter.py -q


# Serial monitor that lets WILL-E read along (tools/serial_buddy.py). make serial NAME=luchtmeter BAUD=115200
NAME ?= esp
BAUD ?= 115200
.PHONY: serial
serial:
	.venv/bin/python tools/serial_buddy.py --name $(NAME) --baud $(BAUD)
