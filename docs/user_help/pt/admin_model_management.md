---
id: admin_model_management
title: "Gestão de modelos de IA e catálogos de fornecedores"
category: settings
keywords:
  - "ativar modelos"
  - "desativar modelos"
  - "seleção múltipla"
  - "sincronização de fornecedores"
  - "descoberta de modelos"
required_role: admin
tool_visible: true
approval_status: approved
last_reviewed: 2026-09-07
locale: pt
base_source_hash: b9815cfdd623e6fea3152e66aef9e4c6a5f32ac62477b1a95a9119179dfb9350
---

## Resposta breve

Ao abrir **Gestão de LLM** ou **Sincronização de fornecedores**, Aurvek atualiza automaticamente catálogos com mais de 24 horas. Mantém a disponibilidade e os ajustes manuais; novos modelos são adicionados desativados. O resultado aparece sem recarregar.

## Passos

1. Abra **Todos os modelos**, aguarde a verificação e filtre por fornecedor, nome, visão ou disponibilidade.
2. Marque linhas ou **Selecionar todos os modelos apresentados** e use **Ativar selecionados** ou **Desativar selecionados**. O interruptor da linha guarda imediatamente.
3. Filtros, ordenação e página mantêm-se; linhas que deixam de corresponder desaparecem e são desmarcadas.
4. Em **Sincronização de fornecedores**, escolha um fornecedor e reveja **Novos**, **Atualizados**, **Em dia** e **Apenas locais**.
5. **Procurar atualizações** deteta novidades; **Atualizar catálogo** adiciona-as desativadas e atualiza metadados. **Novo** significa ainda ausente de Todos os modelos.
6. Para alterar disponibilidade, marque modelos e clique **Guardar alterações**. **Anular alterações** repõe edições pendentes.

## Notas

- Modelos sincronizados podem ser desativados; só modelos manuais podem ser eliminados.
- **Rever** indica metadados incompletos; confirme os preços antes de ativar.
- Datas são partilhadas entre sessões. Um fornecedor falhado mantém o catálogo e pode repetir após uma hora ou imediatamente por atualização manual.
- Modelos geridos automaticamente, como os de contas associadas, não podem ser alterados aqui.
- Ausência na resposta do fornecedor marca o modelo como apenas local, sem o eliminar ou alterar a disponibilidade.

## Relacionado

- chat_model_unavailable
