# ADR-0021: el setter se construye como producto instalable, con una instancia por aliada

- Estado: aceptada
- Fecha: 2026-09-28
- Decide: Dan
- Diseño: [`docs/design/setter-producto-instalable-v1.md`](../design/setter-producto-instalable-v1.md)
- Afina: [ADR-0005](0005-reproducible-client-deployments.md)

## Contexto

El ADR-0005 fijó en julio la arquitectura objetivo: una instalación aislada por cliente, un monorepo, imágenes con versión fijada y cuatro capas separadas. El código no la siguió: la configuración, las ofertas, las plantillas y los textos de la primera aliada quedaron escritos en el código y en migraciones, y la imagen del bridge se construye sin versión.

Con la segunda aliada (ATT1) había dos caminos: adaptar el código de la primera, o construir el producto para que una aliada nueva se instale sin tocar código. El segundo es el que permite que una persona sin el historial del proyecto instale el setter para un negocio nuevo leyendo solo la guía de instalación. Esa es la prueba de aceptación de esta decisión.

## Decisión

1. **Producto e instancia se separan.** El producto (código, SOUL común, esquema, instalador, guía) no contiene datos de ningún cliente. Cada instancia (manifiesto, conocimiento del agente, despliegue) vive fuera del código. Un chequeo del CI falla si aparece en `src/` un valor de un cliente.
2. **Una instancia vive en un repo privado propio**, creado desde un repo plantilla.
3. **Las releases llevan tag semántico y changelog, y publican imágenes en GitHub Container Registry.** EasyPanel usa la imagen por tag; volver atrás es volver al tag anterior. Una versión nueva llega a cada instancia por un PR en su repo, primero a la de menor facturación.
4. **Una instalación nueva arranca de una línea de base de solo estructura.** Ninguna migración nueva inserta datos de un cliente; esos datos entran por el aprovisionamiento, leídos del manifiesto. Las migraciones históricas no se editan.
5. **Los nombres con «johanna» en funciones, tablas y RPCs quedan.** El código nuevo usa nombres genéricos. La regla alcanza a los valores que cambian el comportamiento, no a los nombres.
6. **El SOUL se parte en un SOUL común del producto y un archivo de conocimiento por instancia**, que el bridge valida al arrancar y manda como mensaje `system` en cada pedido. Nace con ATT1 y la primera aliada pasa después (§8.2 del diseño).
7. **La unidad de instancia es la aliada.** Las instancias de una misma empresa pueden compartir Chatwoot (un inbox por aliada), el workspace de Slack (un canal por aliada) y el VPS. Cada instancia tiene propios el bridge, el profile de Hermes, la base y los secretos. Esto afina el ADR-0005, que dibuja un Chatwoot por cliente.
8. **El centro solo entiende eventos canónicos, y cada fuente entra por un adaptador.** Una instancia puede tener código solo en esa ranura, con contrato fijo, y nunca en el centro. Un adaptador se acepta si pasa la suite de conformidad del producto sobre envíos reales capturados de esa fuente. La IA puede escribir el adaptador al construir la instancia; en tiempo de ejecución corre código fijo, como pide el [ADR-0003](0003-deterministic-reasoning-boundary.md).

## Consecuencias

- La instancia sin tráfico va antes que la que factura: ATT1 estrena el camino nuevo y la primera aliada se muda después, sin cambiar su comportamiento. Es la fase de más riesgo del plan y se valida con una semana de números iguales.
- Lo artesanal se permite en el proceso de instalación y nunca en el código: cada valor va al manifiesto y cada paso manual se escribe en la guía en el momento. La guía se prueba con la instalación siguiente a la que la escribió.
- `lead-precheckout-v1` tiene que evolucionar a evento canónico, y la landing de Lancemos pasa a ser el primer adaptador.
- Cada release que cambie el formato del manifiesto es mayor y trae el cambio de manifiesto para cada instancia.
- El agente instalador y los adaptadores de catálogo se construyen después de la primera instalación hecha a mano desde la guía, no antes.

## Alternativas descartadas

- **Adaptar el código de la primera aliada para ATT1.** Resuelve un caso y duplica el problema en el siguiente.
- **Un solo repo con una carpeta por instancia.** Más simple con dos instancias, pero no permite entregarle una instancia a quien la opera sin mostrarle las demás.
- **Guardas dentro de las migraciones con datos** en lugar de una línea de base. Obliga a editar historia ya aplicada.
- **Una instancia por empresa con varias aliadas adentro.** Ahorra servicios, pero obliga a rutear por inbox en todo el código y agranda el alcance de un error.
- **Un modelo que interprete cada webhook en tiempo de ejecución.** Hace no reproducible la entrada de un sistema que manda mensajes y links de pago.
