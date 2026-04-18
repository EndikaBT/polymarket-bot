# Polymarket Bot

Bot de venta automática y copy-trading para Polymarket, con interfaz web local.

## Requisitos

- Python 3.10+
- Una wallet en Polygon con USDC.e y algo de MATIC para gas

## Instalación

```bash
pip install -r requirements.txt
python app.py
```

Abre `http://localhost:5000`. La primera vez pedirá crear una contraseña de acceso.

En Windows también puedes hacer doble clic en `start.bat`.

---

## Acceso remoto con Tailscale

Tailscale permite acceder al bot desde cualquier dispositivo (móvil, otro ordenador) sin abrir puertos en el router ni usar VPN compleja. El tráfico va cifrado punto a punto con WireGuard.

### 1. Instalar Tailscale en el ordenador que corre el bot

Descarga e instala desde [tailscale.com/download](https://tailscale.com/download).

Inicia sesión con una cuenta de Google, GitHub o email:

```
Tailscale → Log in
```

Una vez conectado, el ordenador recibirá una IP privada fija del estilo `100.x.x.x`. Puedes verla en el icono de la bandeja o en [login.tailscale.com/admin/machines](https://login.tailscale.com/admin/machines).

### 2. Instalar Tailscale en el dispositivo desde el que quieres conectarte

Mismo proceso: descarga, instala, inicia sesión **con la misma cuenta**.

Ambos dispositivos aparecerán en el panel de administración y podrán comunicarse directamente.

### 3. Acceder al bot en remoto

Con el bot corriendo en el ordenador principal (`python app.py` o `start.bat`), abre en el otro dispositivo:

```
http://100.x.x.x:5000
```

Sustituye `100.x.x.x` por la IP Tailscale del ordenador principal (la que aparece en el panel de administración).

> El bot escucha en `0.0.0.0:5000`, así que acepta conexiones de la red Tailscale sin ningún cambio adicional.

### 4. (Opcional) Nombre fijo en lugar de IP

En el panel de administración de Tailscale puedes desactivar la rotación de IP y usar el nombre de host del equipo:

```
http://nombre-del-equipo:5000
```

### Seguridad

- El tráfico entre dispositivos Tailscale está cifrado con WireGuard. Nadie en Internet puede acceder al bot aunque el puerto 5000 esté "abierto" en el firewall local — solo los dispositivos de tu cuenta Tailscale llegarán a él.
- El bot tiene autenticación por contraseña, protección contra fuerza bruta (5 intentos → bloqueo 15 min) y tokens CSRF en todas las operaciones.
- **No expongas el puerto 5000 directamente a Internet** (sin Tailscale ni otro proxy). No hay HTTPS, y aunque la autenticación es razonablemente sólida, no está diseñada para estar expuesta públicamente.

### Solución de problemas

| Síntoma | Causa probable | Solución |
|---|---|---|
| No carga la página | El bot no está corriendo | Arranca `start.bat` o `python app.py` en el ordenador principal |
| Conexión rechazada | Firewall de Windows bloqueando el puerto 5000 | Permite Python en el firewall de Windows, o añade una regla para el puerto 5000 |
| Tailscale no conecta | Los dos dispositivos no están en la misma cuenta | Asegúrate de hacer login con la misma cuenta en ambos |
| IP de Tailscale cambió | Rotación normal | Consulta la IP actualizada en el panel de administración |
