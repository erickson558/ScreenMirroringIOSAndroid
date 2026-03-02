import time
from pathlib import Path
from backend.services.uxplay_service import UxPlayService

log_lines=[]
def on_log(msg):
    print(msg)
    log_lines.append(msg)

def on_state(running):
    print('state', running)

svc = UxPlayService(on_log=on_log, on_state_change=on_state)

print('available adapters ->', svc.list_available_interfaces())

ux = Path('tools/uxplay/bin/uxplay.exe')
print('starting default')
try:
    svc.start(ux, 'TestReceiver')
except Exception as e:
    print('start exception', e)
    raise

# let run briefly
for _ in range(4):
    time.sleep(1)
print('stopping default')
svc.stop()

# try with preferred alias if any
adapters = svc.list_available_interfaces()
if adapters:
    alias = adapters[0][0]
    print('starting with alias', alias)
    try:
        svc.start(ux, 'TestReceiver', preferred_interface_alias=alias)
    except Exception as e:
        print('start exception alias', e)
        raise
    for _ in range(4):
        time.sleep(1)
    print('stopping alias')
    svc.stop()

print('done')
