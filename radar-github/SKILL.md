---
name: radar-github
description: "Investigate GitHub repos and register radar candidates."
version: 0.3.0
author: bobymor1-dev, Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [radar, github, candidates, research, triage]
    related_skills: [github, osint-investigation]
---

# Radar GitHub Skill

Recibe enlaces de GitHub, investiga el repositorio (finalidad, mantenimiento,
licencia, seguridad y compatibilidad) y prepara una ficha de candidato para el
registro persistente `bobymor1-dev/hermes-component-radar`, evitando duplicados.

La investigación y el registro local no modifican nada externo. La escritura
remota (Issues, `INVENTARIO.md`) existe pero está tras una puerta de duplicados
**vinculada al candidato**, una comprobación de versión por SHA y una aprobación
humana explícita. La skill nunca instala el candidato.

## When to Use

- El usuario comparte un enlace `https://github.com/<owner>/<repo>` y pide
  revisarlo para Hermes o el Proyecto de Automatización.
- Hay que completar o actualizar una ficha de candidato ya registrada.
- Hay que comprobar si un repositorio ya está en el inventario o en un Issue.

No usar para: instalar dependencias, desplegar código, ni para repositorios que
no sean públicos y técnicos.

## Prerequisites

- Red saliente a `api.github.com`. Sin credencial, la API pública acepta 60
  peticiones/hora y **solo permite leer**.
- **Solo biblioteca estándar de Python 3.10+.** `gh` no es necesario y no debe
  instalarse; `curl` es la alternativa manual documentada.
- El clon local de `bobymor1-dev/hermes-component-radar`, para tener
  `INVENTARIO.md` a mano. Cualquier clon sirve: la ruta se resuelve sola.
- `RADAR_INVENTORY` (opcional) apunta a un `INVENTARIO.md` concreto y tiene
  prioridad sobre la autodetección.

### Credencial (opcional, solo para escribir)

`GH_TOKEN` (preferida) o `GITHUB_TOKEN`. El valor **nunca** se pasa por línea de
comandos, nunca se imprime y nunca se escribe en disco: los scripts leen el
entorno y devuelven únicamente el *nombre* de la variable usada
(`token_source`). Debe ser un token *finegrained* limitado a
`bobymor1-dev/hermes-component-radar` con `Issues: read/write`,
`Contents: read/write` y `Metadata: read`. Sin credencial, todo lo que no sea
lectura falla de forma limpia con código de salida 1.

## How to Run

Lectura y preparación (siempre permitido):

```bash
python3 <skill>/scripts/radar_github.py parse "URL"
python3 <skill>/scripts/radar_github.py inspect OWNER REPO
python3 <skill>/scripts/radar_github.py dedupe OWNER REPO --inventory INVENTARIO.md
python3 <skill>/scripts/radar_github.py card OWNER REPO --data ficha.json --out ficha.md
```

Escritura remota (requiere credencial y `--apply`; sin `--apply` es un *dry-run*
que no escribe nada):

```bash
python3 <skill>/scripts/radar_github_write.py issue-create "Título" \
    --body-file cuerpo.md --candidate OWNER/REPO [--alias "Nombre visible"]
python3 <skill>/scripts/radar_github_write.py issue-comment 7 --body-file nota.md
python3 <skill>/scripts/radar_github_write.py issue-close 8 --body-file cierre.md --state-reason not_planned
python3 <skill>/scripts/radar_github_write.py inventory-get [--full]
python3 <skill>/scripts/radar_github_write.py inventory-append --candidate OWNER/REPO \
    --row "| Candidato | [Issue #N](URL) | ... |"
```

En el dry-run, `issue-create` no envía **nada**; `inventory-append` hace **una
lectura** (`GET` del contenido) porque sin ella no puede informar del `sha`
destino ni del campo `changed`. El resultado lo declara (`reads_in_dry_run`).

Opciones de la puerta, comunes a `issue-create` e `inventory-append`:

- `--candidate OWNER/REPO` (obligatoria): aquello que se está registrando.
- `--inventory RUTA` o `RADAR_INVENTORY`: inventario local a consultar.
- `--remote-inventory`: consulta el `INVENTARIO.md` del registro en lugar de una
  copia local (no necesita clon).
- `--alias NOMBRE` (repetible): nombres visibles alternativos del candidato.
- `--override-gate MOTIVO`: única vía de excepción. El motivo queda registrado en
  el resultado; no existe una opción para saltarse la puerta sin dejar rastro.

## Quick Reference

- Normalizar enlace: `radar_github.py parse "URL"`.
- Resumen de API: `radar_github.py inspect OWNER REPO`.
- Duplicados: `radar_github.py dedupe OWNER REPO --inventory INVENTARIO.md`.
- Redactar ficha: `radar_github.py card OWNER REPO --data ficha.json`.
- Crear/comentar/cerrar Issue, leer y actualizar `INVENTARIO.md`:
  `radar_github_write.py` con `--apply`.

## Veredictos de duplicado

`dedupe` devuelve un campo `verdict` que es la decisión, y un campo `blocked`
que dice si se puede escribir. **Un veredicto no concluyente nunca es un "no
existe".**

| `verdict` | Significado | `blocked` |
|---|---|---|
| `duplicate` | Coincidencia exacta de slug/URL en el inventario, o un Issue cuyo **título** nombra al candidato. | sí |
| `review_required` | Coincidencia solo por *nombre visible* (p. ej. `DonSeTch` ↔ `donsetch`), nombre ambiguo, o un Issue que menciona el nombre sin encabezarlo. Una persona debe confirmarlo. | sí |
| `indeterminate` | No se pudo consultar el inventario **o** los Issues. Ausencia de resultados no es ausencia de duplicados. | sí |
| `not_found` | Se consultaron **ambas** fuentes y ambas vinieron vacías. | no |

- `is_duplicate()` responde solo al caso concluyente (`duplicate`).
- `must_not_create()` es la puerta real: bloquea `duplicate`,
  `review_required` e `indeterminate`.
- `checks_complete` es `True` solo si se consultaron inventario e Issues. Si es
  `False`, el veredicto es `indeterminate` aunque todo parezca limpio.
- Un nombre más corto de 4 caracteres es ambiguo por definición. Un nombre
  coincidente bajo **otro** owner es una colisión, no el mismo candidato.

### Qué cuenta como coincidencia de nombre

La coincidencia de nombre es deliberadamente estrecha, porque los falsos
positivos bloquean candidatos legítimos:

- En una fila de tabla solo cuenta la **columna del nombre** (la primera celda).
  Un nombre mencionado en una celda de descripción, o en la fila de otro
  candidato, no cuenta: `hound` no coincide con una fila de `DonSeTch` que dice
  «Sustituye a Hound».
- Se ignoran los encabezados (`#`, `##`) y las filas separadoras `|---|`, y
  también la **fila de cabecera** de la tabla, para que un candidato llamado
  `Candidato` no coincida con el título de la columna.
- Una línea de prosa solo cuenta si además **parece un registro**: contiene un
  enlace `github.com/…` o una referencia `/issues/N` o `Issue #N`. Así el propio
  texto del documento («Elegir un candidato pendiente») no marca a todos los
  candidatos llamados `candidato`.
- Si el nombre visible no se parece al del repositorio, decláralo con `--alias`.

### Qué cuenta como coincidencia en los Issues

La búsqueda de Issues es **full-text**: encuentra la palabra en cualquier parte
del cuerpo. Por eso los resultados se clasifican por su título:

- El título **empieza** por el nombre del candidato (`DonSeTch — Probar…`) o
  contiene el slug → concluyente (`duplicate`).
- El título **menciona** el nombre sin encabezarlo → indicio
  (`review_required`).
- El título no nombra al candidato y la coincidencia viene del cuerpo → **no es
  evidencia**; se cuenta aparte en `issues.unrelated` y no cambia el veredicto.

Sin esta distinción, un candidato llamado con una palabra corriente queda
declarado `duplicate` para siempre por las fichas ajenas que usan esa palabra.

## Procedure

1. **Recibir y normalizar el enlace.** Extrae `owner`/`repo` con `parse`; si el
   texto no es una URL de repositorio, conserva el `owner/repo` canónico y anota
   la URL original. Criterio: un `slug` canónico y una `html_url` confirmada.
2. **Comprobar duplicados antes de registrar.** Ejecuta `dedupe` contra
   `INVENTARIO.md` y contra los Issues del registro, **para el candidato**
   (`OWNER REPO`), no para el registro. Criterio: veredicto `not_found`.
   **Cualquier otro veredicto impide crear una ficha nueva.** Ante `duplicate`,
   actualiza la ficha existente (el Issue de número más bajo si hay varios). Ante
   `review_required`, pregunta antes de actuar. Ante `indeterminate`, declara la
   incertidumbre y no escribas nada. Si no hay clon local, usa
   `--remote-inventory`: el inventario del registro es la versión que cuenta.
3. **Investigar finalidad.** Lee `README.md` (vía `inspect`, campo `readme`) y usa
   `web_search`/`web_extract` para el sitio y la documentación. Criterio: una frase
   de finalidad y el problema que resuelve.
4. **Investigar mantenimiento.** Observa `pushed_at`, `updated_at`, `archived`,
   `open_issues_count` y `latest_release` del resumen. Criterio: fecha de última
   actividad y última versión/tag verificadas.
5. **Investigar licencia.** Lee el campo `license` (y, si es `other`, el cuerpo de
   `LICENSE`). Criterio: SPDX o nombre de licencia y, en su caso, la restricción
   principal (p. ej. BSL 1.1 con uso no comercial, o AGPL-3.0).
6. **Investigar seguridad.** Comprueba la existencia de `SECURITY.md`, credenciales
   por defecto documentadas, y si el repositorio escribe en `~/.hermes/*` o en
   config compartida. Criterio: riesgos explícitos con evidencia y límites.
7. **Investigar compatibilidad.** Identifica la pila (`language`, `languages`,
   manifiestos) y si es skill/plugin/MCP de Hermes, app externa, biblioteca o
   código de referencia. Criterio: tipo de integración estimado.
8. **Comprobar alternativas nativas de Hermes.** Con `search_files` revisa
   `skills/`, `optional-skills/`, `optional-mcps/`, `plugin-catalog/` y
   `compat_manifest.json` del árbol de Hermes; anota el solapamiento. Criterio:
   una alternativa nativa o la ausencia confirmada.
9. **Clasificar.** Aplica la clasificación técnica (🟢 plug-and-play / 🟢
   autocontenido externo / 🟡 biblioteca o componente / 🔴 código de referencia)
   y un estado (🔎 DESCUBIERTO / 👀 REVISAR / 🧪 PROBAR / ✅ VALIDADO / 🟢 ADOPTADO
   / 🟡 RESERVA / ❌ DESCARTADO). Un precheck documental no es VALIDADO. Criterio:
   clasificación y estado escritos con su motivo.
10. **Preparar la ficha.** Con `card`, genera un Markdown con: nombre y enlace
    original, necesidad y beneficio, clasificación, estado, evidencia, límites,
    prioridad, prueba suficiente, resultado y siguiente paso.
11. **Registrar.** Con `verdict=not_found` y aprobación humana: crea el Issue
    (`issue-create --candidate OWNER/REPO`, que vuelve a pasar la puerta y la
    vincula al candidato) y añade la fila en `INVENTARIO.md`
    (`inventory-append --candidate OWNER/REPO`, que pasa la misma puerta). Ambas
    operaciones se verifican leyendo el resultado remoto. Criterio: la fila
    existe, el Issue existe y no hay duplicados.
12. **Consolidar duplicados ya existentes.** Deja el Issue de número más bajo
    como ficha canónica: comenta la trazabilidad en el canónico y luego cierra el
    duplicado con `issue-close --state-reason not_planned` y un comentario que
    explique el motivo. Nunca borres: el cierre conserva título, cuerpo, autor y
    fechas, y GitHub no permite borrar Issues por API.

## Escritura remota: garantías

- **Dry-run por defecto.** Sin `--apply` no se **escribe** nada. `issue-create`,
  `issue-comment` y `issue-close` no envían ninguna petición; `inventory-append`
  hace una lectura para poder informar del `sha` destino (`reads_in_dry_run`).
- **Puerta de duplicados vinculada al candidato.** `issue-create` y
  `inventory-append` ejecutan `dedupe` **sobre el candidato** (`--candidate`), no
  sobre el registro, y se niegan si `must_not_create()` es cierto, sin llamar a
  la API de escritura. El resultado de `dedupe` se acepta solo si su `slug`
  coincide con el candidato que se está creando: un veredicto sobre otro
  repositorio no autoriza nada.
- **La puerta es obligatoria.** Sin resultado de `dedupe` y sin
  `--override-gate MOTIVO` no se crea nada. La vía de excepción es explícita y
  queda registrada en el resultado (`gate.override_reason`).
- **`inventory-append` es idempotente.** Si la fila ya está idéntica, no hace
  nada (`changed: false`), de modo que un reintento no registra dos veces. Si se
  pasa `--anchor` y no aparece, es un error: no adivina en qué tabla va.
- **Control de versión.** `INVENTARIO.md` se actualiza con el `sha` del blob. Si
  otro escritor se adelanta, la API devuelve 409 y el reintento **vuelve a
  aplicar la transformación sobre el contenido fresco**, de modo que la edición
  ajena sobrevive.
- **`require_unchanged_base`.** Para reemplazos completos: si el fichero cambió
  desde la lectura, aborta en lugar de pisar.
- **Verificación por relectura, sin falso fallo.** Tras cada escritura se lee de
  nuevo el objeto remoto y se compara. Si la escritura **sabemos** que se aplicó
  pero la relectura no puede confirmarla (otro escritor entró, o falló la
  lectura), el resultado es `applied: true, verified: false` con un motivo en
  `verification`, **no** una excepción: la escritura ya está hecha y llamarlo
  fallo empujaría a reintentar y duplicar. `VerificationError` queda para
  respuestas inservibles (sin número de Issue, JSON inválido).
- **Códigos de salida.** `0` hecho · `1` falló · `3` la puerta de duplicados
  rechazó (`radar_github.py dedupe` también usa `3` cuando `blocked` es cierto) ·
  `4` se aplicó pero no se pudo verificar.
- **Credenciales.** Solo desde el entorno. Un valor con salto de línea o
  caracteres no ASCII se rechaza antes de usarlo, porque `urllib` lo incrustaría
  en un `ValueError` con el valor citado; `redact()` limpia cualquier mensaje
  emitido y `main()` convierte toda excepción inesperada en una línea redactada,
  nunca en un traceback.

## Mantenimiento autónomo

Sin aprobación adicional: `parse`, `inspect`, `dedupe`, `card`, `inventory-get`
y los dry-run.

Requiere aprobación humana explícita, siempre: crear un Issue, comentar, cerrar,
modificar `INVENTARIO.md`, cualquier `git push`, e instalar cualquier componente.

Ciclo seguro sugerido:

```bash
git -C <clon> fetch && git -C <clon> pull --ff-only
python3 <skill>/scripts/radar_github.py dedupe OWNER REPO --inventory <clon>/INVENTARIO.md
# proponer el cambio y esperar la aprobación
python3 <skill>/scripts/radar_github_write.py issue-create "..." --body-file cuerpo.md --candidate OWNER/REPO   # dry-run
python3 <skill>/scripts/radar_github_write.py issue-create "..." --body-file cuerpo.md --candidate OWNER/REPO --apply
```

Tras cada `--apply`, comprobar el resultado leyendo de vuelta (el propio script
lo hace) y registrar en el mensaje: número de Issue, `sha` del commit y
resultado.

## Pitfalls

- El campo `license` de la API devuelve `other`/`NOASSERTION` para licencias no
  estándar (BSL, SSPL, etc.); hay que leer `LICENSE` para no malinterpretarlas.
- La ausencia de `SECURITY.md` no prueba inseguridad; regístrala como "no
  documentado", no como vulnerabilidad.
- Un Issue cerrado o un `total_count` de búsqueda no exime de revisar
  `INVENTARIO.md` local, que puede estar por delante del remoto.
- No deducir clasificación ni estado del nombre ni de la popularidad.
- **Un candidato puede estar en `INVENTARIO.md` solo por su nombre visible, sin
  slug.** Por eso existe el veredicto `review_required`: no lo trates como
  "no registrado".
- **Si la búsqueda de Issues falla, el resultado es `indeterminate`, nunca
  `not_found`.** No traduzcas un fallo de red a "no existe".
- Un veredicto `review_required` o `indeterminate` bloquea la escritura igual que
  `duplicate`: la diferencia es el motivo, no el permiso.
- Al reintentar una escritura de `INVENTARIO.md`, no reenvíes el texto calculado
  antes del conflicto: perderías la edición ajena. Pasa siempre una función de
  transformación.
- **La búsqueda de Issues es full-text y encuentra palabras en el cuerpo.** Un
  `total_count` alto no prueba nada sobre el candidato: mira `issues.matches`
  (título concluyente) y `issues.suggestive`. `issues.unrelated` recoge lo que
  solo coincidía por el texto y no cambia el veredicto.
- **Un nombre corriente (`radar`, `candidato`) aparece en la prosa del propio
  documento.** Por eso las líneas de prosa solo cuentan si además parecen un
  registro, y en las tablas solo cuenta la columna del nombre. Si aun así hay
  dudas, declara el nombre visible con `--alias`.
- **Una escritura que se aplicó y no se pudo confirmar no es un fallo.** El
  resultado trae `applied: true, verified: false` y un motivo: comprueba el
  estado remoto antes de reintentar, o el reintento duplicará la ficha.
- **No uses un `--anchor` aproximado.** Si no aparece literalmente en el
  fichero, la operación falla a propósito: es mejor fallar que escribir la fila
  en la tabla equivocada.
- **No inventes clasificación, estado ni prioridad a partir de la popularidad.**
  Un precheck documental no es VALIDADO.

## Verification

- `python3 -m unittest discover -s <skill>/tests -q` en verde (133 pruebas).
- `radar_github.py parse` devuelve `owner/repo` para una URL real.
- `radar_github.py inspect` devuelve licencia, `pushed_at` y `archived` reales.
- `radar_github.py dedupe` devuelve `duplicate` para un candidato ya listado por
  slug o con un Issue cuyo título lo nombra, `review_required` para uno listado
  solo por nombre, `indeterminate` con la red caída, y `not_found` solo tras
  consultar ambas fuentes. Sale con código `3` cuando `blocked` es cierto.
- `radar_github_write.py <cmd>` sin `--apply` no escribe nada; `issue-create`
  exige `--candidate` y se niega si la puerta está bloqueada o ausente.
- Las pruebas no tocan la red: sendos `setUpModule` sustituyen los transportes
  por stubs que fallan ruidosamente ante cualquier llamada real.
- La batería no es vacía: revertir cualquiera de las garantías (vínculo al
  candidato, puerta obligatoria, puerta en `inventory-append`, idempotencia,
  validación de credencial, columna de nombre, fila de cabecera, clasificación
  de Issues, informe `applied`-sin-verificar) pone pruebas en rojo.
