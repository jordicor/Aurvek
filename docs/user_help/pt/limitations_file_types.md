---
id: limitations_file_types
title: "Tipos de ficheiro suportados e limites de carregamento"
category: limitations
keywords:
  - "ficheiro"
  - "carregar"
  - "imagem"
  - "PDF"
  - "tipo"
  - "limite"
  - "formatos suportados"
  - "anexo"
  - "texto"
  - "código"
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-09-03
locale: pt
base_source_hash: 6b207a41121e1dbaf8d1aae712481d6f0c45c0f94500a2340aed5d426f15012d
---

## Resposta breve

Aurvek aceita imagens, PDFs e texto/código suportados. Não aceita Word, folhas de cálculo, áudio, vídeo ou arquivos comprimidos. O carregamento deve estar autorizado para a conta e conversa.

## Notas

- Aceita imagens comuns, PDF, TXT, Markdown, CSV, JSON, XML, HTML, Python, JavaScript/TypeScript, CSS, SQL, YAML, configuração, registos, shell e extensões de código comuns.
- Imagens: até 10, menos de 20 MB cada e 50 megapíxeis; tenta reduzi-las ao limite do fornecedor.
- PDF: até 3, menos de 25 MB cada e 1.000 páginas por mensagem.
- Texto/código: até 3, menos de 2 MB cada. Total combinado: 16 anexos.
- A área de transferência só aceita imagens.
- xAI (Grok) converte WebP para JPEG; PDFs com GPT/xAI seguem automaticamente por OpenRouter.
- É uma permissão da conta; contacte o administrador se faltar o botão.
- Multi-IA e GranSabio não aceitam anexos.

## Relacionado

- limitations_unsupported_features
- limitations_free_models
- file_uploads
