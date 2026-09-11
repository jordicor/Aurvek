---
id: admin_model_management
title: "Gestión de modelos de IA y catálogos de proveedores"
category: settings
keywords:
  - "activar modelos"
  - "desactivar modelos"
  - "selección múltiple"
  - "sincronización de proveedores"
  - "descubrimiento de modelos"
required_role: admin
tool_visible: true
approval_status: approved
last_reviewed: 2026-09-07
locale: es
base_source_hash: b9815cfdd623e6fea3152e66aef9e4c6a5f32ac62477b1a95a9119179dfb9350
---

## Respuesta breve

Al abrir **Gestión de LLM** o **Sincronización de proveedores**, Aurvek actualiza automáticamente los catálogos con más de 24 horas. Conserva la disponibilidad y los ajustes manuales existentes; los modelos nuevos se añaden desactivados. El resultado aparece sin recargar la página.

## Pasos

1. Abre **Todos los modelos**, espera la comprobación y filtra por proveedor, nombre, visión o disponibilidad.
2. Marca filas o **Seleccionar todos los modelos mostrados** y usa **Activar seleccionados** o **Desactivar seleccionados**. El interruptor de cada fila guarda al instante.
3. Los filtros, el orden y la página se conservan. Las filas que dejan de coincidir desaparecen y se desmarcan.
4. En **Sincronización de proveedores**, elige un proveedor y revisa **Nuevos**, **Actualizados**, **Al día** y **Solo locales**.
5. **Buscar actualizaciones** detecta novedades desde la actualización automática. **Actualizar catálogo** las añade desactivadas y actualiza sus metadatos. **Nuevo** significa que aún no está en Todos los modelos.
6. Para cambiar la disponibilidad, marca o desmarca modelos y pulsa **Guardar cambios**. **Deshacer cambios** restablece las ediciones pendientes antes de actualizar el catálogo.

## Notas

- Los modelos sincronizados se pueden desactivar; solo los manuales se pueden eliminar.
- **Revisar** indica metadatos incompletos; comprueba el precio antes de activar el modelo.
- Las fechas de actualización se comparten entre sesiones. Si un proveedor falla, conserva su catálogo y puede reintentarlo tras una hora o inmediatamente con una actualización manual.
- Los modelos de gestión automática, como los de cuentas vinculadas, no se pueden cambiar aquí.
- Un modelo ausente de la respuesta del proveedor queda como solo local; no se elimina ni cambia su disponibilidad automáticamente.

## Relacionado

- chat_model_unavailable
