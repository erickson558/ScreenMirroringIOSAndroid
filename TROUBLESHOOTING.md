# Resolución de Problemas: iPhone No Detecta el PC

## Problema
El iPhone no detecta este PC para establecer una conexión AirPlay, incluso cuando el receptor está corriendo.

## Causa Raíz
Sin el servicio Bonjour/mDNS en Windows, el iPhone no puede descubrir el receptor en la red local. UxPlay *requiere* Bonjour para funcionar.

## Solución: Instalar Bonjour en Windows

### Pasos:

1. **Descarga Bonjour instalador** desde Apple:
   - Ir a: https://support.apple.com/downloads/bonjour

2. **Instala Bonjour**:
   - Ejecuta el instalador `.exe`
   - Sigue los pasos del asistente
   - Reinicia tu equipo después de instalar

3. **Verifica que Bonjour está corriendo**:
   - Abre un terminal PowerShell como administrador
   - Ejecuta:
     ```powershell
     sc query "Bonjour Service"
     ```
   - Deberías ver `STATE : 4 RUNNING`

4. **Reinicia el receptor de AirPlay**:
   - Abre la aplicación ScreenMirrorIOSAndroid
   - Inicia el receptor
   - El iPhone debería detectar el PC en AirPlay

---

## Verificación: Ejecutar Diagnóstico

Para validar que la red y Bonjour están funcionando correctamente:

```bash
python scripts/diagnose_network.py
```

Debería mostrar:
- ✓ mDNS multicast group joined successfully
- ✓ mDNS query sent
- ✓ mDNS response received (o muy cercano)
- ✓ Bonjour Service is RUNNING
- ✓ Todos los puertos de UxPlay escuchando

---

## Problemas Adicionales

### "Firewall tiene BlockInbound"
- El firewall está configurado para bloquear conexiones entrantes
- Solución: Permite UxPlay manualmente en Firewall de Windows
  - Control Panel > Windows Defender Firewall > Allow an app through firewall
  - Asegúrate de que `uxplay.exe` esté permitido (tanto privadas como públicas)

### "Wi-Fi en modo Público"
- Cambia la red a "Privada":
  - Settings > Network & Internet > Wi-Fi > Manage > [Tu red]
  - Cambiar a Network profile: Private

### "VPN Activa"
- Las VPNs pueden bloquear tráfico local (mDNS)
- Desconecta la VPN, inicia el receptor, luego reconecta

---

## Alternativas si Bonjour no funciona

Si incluso after instalar Bonjour el iPhone aún no detecta:

1. **Deshabilita temporalmente el Firewall** para verificar:
   ```powershell
   Set-NetFirewallProfile -Profile Private -Enabled $false
   ```
   - Si funciona, entonces es problema del firewall

2. **Verifica que las interfaces de red están en el mismo rango**:
   - iPhone y PC deben estar en la misma Wi-Fi
   - Ambos deben tener IPs en el mismo rango (ej: 192.168.x.x)

3. **Reinicia el router de Wi-Fi** para limpiar la tabla ARP

---

## Logs para Enviar
Si aún tienes problemas, copia los logs desde la pestaña "Logs" de la app y incluye:
- Salida del diagnóstico (`python scripts/diagnose_network.py`)
- Los warnings mostrados en la app al iniciar el receptor
