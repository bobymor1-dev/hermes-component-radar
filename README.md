# hermes-component-radar
Radar de herramientas, MCP, skills y componentes candidatos para Hermes Agent y el Proyecto de Automatización.
hermes-component-radar

Radar de herramientas, MCP, skills y componentes candidatos para Hermes Agent y el Proyecto de Automatización.

Objetivo

Encontrar y conservar componentes que ayuden a mejorar o proteger ingresos, ahorrar tiempo y resolver necesidades reales de Agente 0 con poco esfuerzo de integración y mantenimiento.

Dos vías de entrada

- Exploración: descubrir componentes mediante el radar de repositorios.
- Necesidad concreta: buscar una solución para un obstáculo observado en una tarea real.

Ambas vías alimentan una única lista de prioridades. Mantenemos una sola mejora en construcción cada vez.

Método

1. Recuperar lo que ya existe y las pruebas anteriores.
2. Reutilizar lo instalado; después buscar componentes, adaptar y desarrollar solo si hace falta.
3. Definir una prueba pequeña, suficiente, aislada y reversible.
4. Probar con una tarea real.
5. Registrar resultado y decidir: incorporar, corregir o aparcar.
6. Automatizar únicamente cuando el procedimiento funcione.

Prueba práctica primero; benchmark solo cuando quede una duda importante que necesitemos resolver.

Registro de candidatos

Usamos un Issue por candidato. Antes de crearlo, comprobamos si ya existe.

Cuando el usuario comparte un enlace, nombre o captura y pide revisarlo para Hermes o Automatización, corresponde incorporarlo o actualizar su ficha.

Cada ficha recoge:

- Nombre y enlace original; si no está confirmado, indicarlo.
- Necesidad que resuelve y beneficio esperado.
- Clasificación técnica y esfuerzo de integración estimado.
- Estado, evidencia disponible y límites.
- Prioridad provisional y motivo.
- Prueba suficiente.
- Resultado, cuando exista.
- Siguiente paso y destino exacto de ejecución.

Clasificación técnica

- 🟢 Plug-and-play Hermes: preparado para incorporarse mediante un mecanismo compatible, pendiente de comprobar requisitos.
- 🟢 Autocontenido externo: funciona como aplicación o servicio separado; puede requerir conexión con Hermes.
- 🟡 Biblioteca/componente con integración: necesita adaptación o configuración para utilizarse.
- 🔴 Código de referencia/desarrollo: requiere trabajo de desarrollo sustancial.

La clasificación describe el tipo de integración; no certifica seguridad ni funcionamiento.

Estados

- 🔎 DESCUBIERTO: registrado, pendiente de evaluación.
- 👀 REVISAR: necesita aclaraciones o revisión.
- 🧪 PROBAR: candidato a una prueba práctica.
- ✅ VALIDADO: funcionó y aportó valor en una prueba documentada.
- 🟢 ADOPTADO: forma parte estable del sistema.
- 🟡 RESERVA: conservado para una necesidad futura.
- ❌ DESCARTADO: rechazado, con motivo registrado.

Un precheck documental no equivale a VALIDADO. VALIDADO no equivale a ADOPTADO.

Prioridad

El ranking sirve para elegir qué probar, no para decidir adopciones.

Priorizamos beneficio económico, ahorro recurrente de tiempo, capacidad para desbloquear tareas y reutilización. Consideramos también coste, esfuerzo y mantenimiento.

Las prioridades iniciales son provisionales. Descubrir un componente no autoriza su instalación.

Operación temporal

GitHub es el registro persistente. Utilizamos README, Issues y chats, sin añadir otra base de datos.

Mientras la integración no permita escribir, ChatGPT prepara el contenido y el usuario lo traslada manualmente. Cada entrega indica repositorio, rama y ruta de archivo, o destino en Issues, además de la acción y comprobación esperada.

El repositorio permanece público temporalmente. Solo se publica contenido técnico sin datos personales sensibles ni secretos.

Límites

Registrar un candidato no modifica Hermes, Gateway, proveedor, routing ni canon.

Las instalaciones se ejecutan en el entorno y chat de construcción correspondiente. Cada cambio operativo requiere su propia evidencia.