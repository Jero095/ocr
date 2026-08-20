# Tailscale: cómo se publica el app y cómo se da acceso

Este app corre en una PC, no en un servidor. Tailscale es lo que lo hace
alcanzable desde otros equipos sin exponerlo a internet: arma una red privada
entre dispositivos autorizados y entrega HTTPS con certificado válido, sin
comprar dominio y sin abrir puertos en el router.

> Este documento está en español porque es un procedimiento operativo. El resto
> de la documentación del repo está en inglés.

---

## Estado actual

| | |
|---|---|
| URL del app | `https://desktop-b2pkpab.tail0aee04.ts.net` |
| Tailnet | `tail0aee04.ts.net`, dueño `jaureguijeronimo@gmail.com` |
| Esta PC | `desktop-b2pkpab` · `100.103.52.1` |
| Otro equipo | `desktop-4dvunj8` (`m.castro@`) · `100.65.45.69` |
| Versión | 1.102.2 |
| Exposición | `tailnet only` — **no** está en internet público |

La URL no es un secreto (sin estar en la tailnet no resuelve ni conecta), pero
tampoco hace falta repartirla fuera del equipo.

---

## Instalación

En la PC que hospeda el app:

```bash
winget install Tailscale.Tailscale
```

Después iniciar sesión desde la app de Tailscale (abre el navegador). En cada
equipo que necesite entrar: instalar Tailscale y entrar **con su propia cuenta**
— nadie comparte credenciales.

Para que `tailscale serve` pueda emitir el certificado HTTPS hay que habilitar,
una sola vez, en [login.tailscale.com/admin/dns](https://login.tailscale.com/admin/dns):

1. **MagicDNS**
2. **HTTPS Certificates**

Si no están activados, el paso siguiente falla.

### El binario no siempre queda en el PATH

En una terminal ya abierta, `tailscale` puede no encontrarse. El ejecutable está en:

```bash
export PATH="$PATH:/c/Program Files/Tailscale"
```

(`app/ocr.py` resuelve lo mismo para Tesseract con `_find_tesseract()`.)

---

## Publicar el app

Primero el app tiene que estar corriendo en el puerto 8000:

```bash
python -m uvicorn app.main:app --port 8000
```

Después, en una terminal **como Administrador**:

```bash
tailscale serve --bg 8000
```

`--bg` lo deja corriendo en segundo plano. Verificar:

```bash
tailscale serve status
```

Tiene que decir `(tailnet only)` y apuntar a `http://127.0.0.1:8000`.

La configuración de `serve` **sobrevive reinicios**; uvicorn no. Por eso después
de reiniciar la PC el app responde 502 hasta que se levanta uvicorn de nuevo.

### Dejar de publicar

```bash
tailscale serve --https=443 off
```

O `tailscale serve reset` para borrar toda la configuración de serve.

---

## Dar acceso a otra persona

Hay dos caminos. En esta tailnet se usó el primero.

### A. Invitar a la persona como usuario de la tailnet

En [login.tailscale.com/admin/users](https://login.tailscale.com/admin/users) →
**Invite users** → su correo. La persona acepta, instala Tailscale y entra con su
cuenta. El plan Personal admite hasta 3 usuarios.

Su equipo aparece en **Machines**, posiblemente con el estado `Restricted` y la
etiqueta *"Owner needs approval"*. Eso es **device approval**: hay que autorizarlo.

**Cómo aprobar:** [login.tailscale.com/admin/machines](https://login.tailscale.com/admin/machines)
→ fila del equipo → menú `...` → **Approve**.

Si la opción no aparece, casi siempre es que la consola está abierta con **otra
cuenta de Google**. Las acciones de admin solo se muestran al dueño de la tailnet,
que acá es `jaureguijeronimo@gmail.com`. Verificar arriba a la derecha con qué
correo está la sesión.

Para dejar de aprobar uno por uno: **Settings → Device management → Device
approval** (desactivarlo). Se pierde ese control, así que conviene dejarlo puesto.

### B. Compartir solo este equipo

Si no querés que la persona sea usuario de la tailnet:
**Machines** → `desktop-b2pkpab` → `...` → **Share…** → enviar el enlace. Acepta
con su propia cuenta y obtiene acceso a **ese equipo únicamente**.

Nota: en dispositivos compartidos, MagicDNS puede no resolver el nombre. El
respaldo es la IP (`https://100.103.52.1`), que funciona pero rompe el
certificado. Conviene probarlo antes de depender de eso.

### Quitar el acceso

- Un equipo: **Machines** → `...` → **Remove**
- Un usuario: **Users** → `...` → **Suspend**
- Solo la app, sin tocar la red: `python scripts/adduser.py disable <email>`
  (revoca las sesiones al instante)

---

## Diagnóstico

```bash
tailscale status
```

Lista los equipos, quién es dueño de cada uno y si están online. Un equipo que no
aparece acá no está en la tailnet (típicamente falta aprobarlo).

```bash
tailscale netcheck
```

Muestra si el NAT permite conexión directa y la latencia a los relays. En esta
red: `UDP: true` y `MappingVariesByDestIP: false` — NAT permisivo, o sea conexión
directa punto a punto. El relay más cercano es Miami a 88.5 ms, y se usa solo si
la conexión directa falla.

```bash
tailscale ping desktop-4dvunj8
```

Dice si el tráfico va **directo** o **por relay (DERP)**.

```bash
tailscale serve status      # qué se está publicando
tailscale lock status       # Tailnet Lock (acá: NOT enabled)
tailscale version
```

---

## Problemas comunes

**502 Bad Gateway.** Tailscale funciona; uvicorn no está corriendo. `serve`
persiste entre reinicios y el app no. Levantar uvicorn.

**No entra desde otro equipo.** Revisar en orden: (1) el equipo aparece en
`tailscale status`, (2) está aprobado y no dice `Restricted`, (3) esta PC está
despierta, (4) uvicorn está arriba.

**La PC dormida.** Es el límite real de hospedar en casa: si esta PC suspende, el
app no existe para nadie. Se ajusta en las opciones de energía de Windows.

**Certificado inválido.** Suele ser que se entró por IP en vez del nombre, o que
faltan MagicDNS / HTTPS Certificates en la consola.

---

## `tailscale funnel` — no usar

`funnel` es el hermano de `serve` que publica el app **en internet público**,
accesible por cualquiera con la URL. Está deliberadamente apagado.

Se menciona acá solo para que no se ejecute por error. Si algún día hace falta
acceso público, primero conviene revisar el login del app: hoy protege bien un
grupo chico y conocido, pero no está endurecido contra internet abierto.

---

## Lo que Tailscale no resuelve

Tailscale controla **quién puede llegar** al app. No controla qué puede hacer una
vez dentro, ni deja registro de quién hizo qué. Eso es el login
(`app/auth.py`) y el historial atribuido (`app/store.py`), que se construyeron
justamente por eso.

Dicho de otra forma: aprobar un dispositivo da acceso total a todos los
statements. Las cuentas son lo que permite revocar a una persona sin sacar el
equipo de la red, y lo que hace que quede registrado quién subió cada statement.

Dos cosas que Tailscale tampoco cubre y siguen pendientes:

- **Arranque automático** de uvicorn al prender la PC (por eso aparece el 502).
- **Backup de `data/`**, que hoy es la única copia del historial.
