from pathlib import Path
import sys
from pathlib import Path as P

# Ensure project root is on sys.path
root = P(__file__).resolve().parent.parent
sys.path.insert(0, str(root))

from backend.services.capture_service import CaptureService

uxplay = Path(r"D:/OneDrive/Regional/1 pendientes para analisis/proyectospython/LonelyScreenIOS/tools/uxplay/bin/uxplay.exe")
output = Path(r"C:/Users/erickson/AppData/Local/Temp/test_snapshot.png")
svc = CaptureService(on_log=print)
try:
    print('Candidate sources:', svc._window_capture_sources('UxPlay'))
    svc.take_snapshot(uxplay_path=uxplay, output_path=output, source_mode='window', window_title='UxPlay')
    print('Snapshot saved to', output)
except Exception as e:
    print('Snapshot failed:', e)
