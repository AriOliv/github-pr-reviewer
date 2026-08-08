# Auto PR Generator

Este repositório conta com uma automação baseada em Inteligência Artificial para gerar Pull Requests automaticamente a partir de Issues.

## Workflow Issue to PR

O workflow **Issue to PR** automatiza o processo de desenvolvimento da seguinte forma:

1. **Abertura da Issue**: Uma nova issue é criada descrevendo uma tarefa, bug ou melhoria necessária.
2. **Processamento**: O GitHub Actions captura os detalhes da issue (título e descrição) e aciona o agente de IA.
3. **Geração da Solução**: O agente analisa o código do repositório e gera os arquivos alterados ou novos arquivos necessários para atender à issue.
4. **Criação do Pull Request**: Um Pull Request é aberto automaticamente com as alterações propostas e referenciando a issue original para revisão.
