# ScreenMirrorIOSAndroid (Python)

Version actual: `V0.0.84`

Aplicacion de escritorio en Python con arquitectura separada (`backend`/`frontend`) para espejo de pantalla con iPhone (AirPlay/UxPlay) y Android (Proyeccion inalambrica/Miracast).

## Funcionalidades

- Interfaz estilo Aero en espanol.
- Selector de dispositivo con `Radiobutton`: `iPhone` / `Android`.
- Guardado automatico de configuracion de la GUI en `config.json`.
- Atajos de teclado estilo Windows con letra subrayada (`Alt + tecla`).
- Barra de menu (`Archivo`/`Ayuda`) y ventana `Acerca de` con version.
- Barra de estado para mensajes de la aplicacion.
- Captura de imagen con seleccion de ruta de guardado.
- Grabacion de video `.mp4`.
- Perfiles de transmision para estabilidad y menor latencia.
- Registro en tiempo real con pistas de diagnostico.
- Anuncio AirPlay multi-interfaz en Windows.
- Diagnostico Android/Miracast con pistas de configuracion de Windows.
- Versionado en `version.json` con formato `Vx.x.x`.
- Log en `log.txt` con timestamp.

## Nota importante

AirPlay/Miracast en este flujo es recepcion de audio/video. No hay control tactil directo del telefono desde la ventana.

## Requisitos

- Windows + Python 3.10+
- Runtime de UxPlay en:
  - `tools/uxplay/bin/uxplay.exe`
  - `tools/uxplay/lib/...`

## Ejecucion en desarrollo

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

## Modo Android y Miracast

- Al pulsar `Abrir proyeccion Android`, la app abre la configuracion de proyeccion de Windows.
- Si el equipo no admite recibir Miracast, Android no encontrara el receptor.
- Si Samsung no detecta el PC, revisa `Proyeccion en este equipo`, confirma que `Wireless Display` este instalado y verifica que Windows permita la disponibilidad del receptor.

## Validacion rapida

```powershell
pytest -q
python scripts\diagnose_network.py
```

## Compilar EXE en la misma carpeta del `.py`

```powershell
.\build_exe.ps1
```

Resultado:

- `ScreenMirrorIOSAndroid.exe` en la raiz del proyecto.
- Icono cargado desde `ScreenMirrorIOSAndroid.ico`.
- Runtime `tools/uxplay` embebido en el ejecutable `onefile`.
- Incremento automatico de version en `version.json`.

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
