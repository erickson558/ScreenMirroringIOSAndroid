# ScreenMirrorIOSAndroid (Python)

AplicaciÃ³n de escritorio en Python con arquitectura separada (`backend`/`frontend`) para espejo de pantalla con iPhone (AirPlay/UxPlay) y Android (ProyecciÃ³n inalÃ¡mbrica/Miracast).

## Funcionalidades

- Interfaz estilo Aero en espaÃ±ol.
- Selector de dispositivo con `Radiobutton`: `iPhone` / `Android`.
- Guardado automÃ¡tico de configuraciÃ³n de la GUI en `config.json`.
- Atajos de teclado estilo Windows con letra subrayada (`Alt + tecla`).
- Barra de menÃº (`Archivo`/`Ayuda`) y ventana `Acerca de` con versiÃ³n.
- Barra de estado para mensajes de la aplicaciÃ³n (sin `messagebox`).
- Captura de imagen con selecciÃ³n de ruta de guardado.
- GrabaciÃ³n de video `.mp4`.
- Perfiles de transmisiÃ³n para estabilidad y menor latencia.
- Registro en tiempo real con pistas de diagnÃ³stico.
- Anuncio AirPlay multi-interfaz en Windows (si hay VPN/Wi-Fi/LAN activas, crea una instancia por adaptador).
- Versionado en `version.json` con incremento automÃ¡tico al compilar.
- Log en `log.txt` con timestamp.

## Nota importante

AirPlay/Miracast en este flujo es recepciÃ³n de audio/video. No hay control tÃ¡ctil directo del telÃ©fono desde la ventana.

## Requisitos

- Windows + Python 3.10+
- Runtime de UxPlay en:
  - `tools/uxplay/bin/uxplay.exe`
  - `tools/uxplay/lib/...`

## EjecuciÃ³n en desarrollo

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

## Modo Android y Miracast

- Al pulsar `Abrir proyecciÃ³n Android`, la app abre la configuraciÃ³n de proyecciÃ³n de Windows.
- Si el equipo no admite recibir Miracast, Android no encontrarÃ¡ el receptor. La app lo reporta con diagnÃ³stico.

## Compilar EXE en la misma carpeta del `.py`

```powershell
.\build_exe.ps1
```

Resultado:

- `ScreenMirrorIOSAndroid.exe` en la raÃ­z del proyecto (no en `dist`)
- Ãcono cargado desde `ScreenMirrorIOSAndroid.ico`
- Runtime `tools/uxplay` embebido en el ejecutable `onefile`
- Incremento automÃ¡tico de versiÃ³n (`version.json`, +`0.0.1` por compilaciÃ³n)

## Troubleshooting Git SSL (Windows)

Si aparece el error:

`No se pudo cargar la URL: SSL certificate problem: unable to get local issuer certificate`

ejecuta:

```powershell
.\scripts\fix_git_ssl_windows.ps1
```

El script aplica configuracion recomendada para Git en Windows:

- Usa `schannel` (almacen de certificados de Windows).
- Limpia overrides de CA (`http.sslCAInfo`, `http.sslCAPath`).
- Mantiene `http.sslVerify=true`.

Solo como ultimo recurso temporal:

```powershell
.\scripts\fix_git_ssl_windows.ps1 -InsecureFallback
```

Eso desactiva validacion SSL (`http.sslVerify=false`) y no se recomienda para uso permanente.
