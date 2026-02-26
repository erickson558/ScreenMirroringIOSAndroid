# LonelyScreenIOS (Python)

Aplicación de escritorio en Python con arquitectura separada (`backend`/`frontend`) para espejo de pantalla con iPhone (AirPlay/UxPlay) y Android (Proyección inalámbrica/Miracast).

## Funcionalidades

- Interfaz estilo Aero en español.
- Selector de dispositivo con `Radiobutton`: `iPhone` / `Android`.
- Guardado automático de configuración de la GUI en `config.json`.
- Atajos de teclado estilo Windows con letra subrayada (`Alt + tecla`).
- Barra de menú (`Archivo`/`Ayuda`) y ventana `Acerca de` con versión.
- Barra de estado para mensajes de la aplicación (sin `messagebox`).
- Captura de imagen con selección de ruta de guardado.
- Grabación de video `.mp4`.
- Perfiles de transmisión para estabilidad y menor latencia.
- Registro en tiempo real con pistas de diagnóstico.
- Versionado en `version.json` con incremento automático al compilar.
- Log en `log.txt` con timestamp.

## Nota importante

AirPlay/Miracast en este flujo es recepción de audio/video. No hay control táctil directo del teléfono desde la ventana.

## Requisitos

- Windows + Python 3.10+
- Runtime de UxPlay en:
  - `tools/uxplay/bin/uxplay.exe`
  - `tools/uxplay/lib/...`

## Ejecución en desarrollo

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

## Modo Android y Miracast

- Al pulsar `Abrir proyección Android`, la app abre la configuración de proyección de Windows.
- Si el equipo no admite recibir Miracast, Android no encontrará el receptor. La app lo reporta con diagnóstico.

## Compilar EXE en la misma carpeta del `.py`

```powershell
.\build_exe.ps1
```

Resultado:

- `LonelyScreenIOS.exe` en la raíz del proyecto (no en `dist`)
- Ícono cargado desde `lonelyscreenIOS.ico`
- Runtime `tools/uxplay` embebido en el ejecutable `onefile`
- Incremento automático de versión (`version.json`, +`0.0.1` por compilación)
