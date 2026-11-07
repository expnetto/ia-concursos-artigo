# Desvios do previsto e limitações

## Hipóteses

- **H3** não foi testada, por falta de crédito de API.
- **H5** não foi testada na forma registrada.
- **H6:** a abstenção útil registrada não pôde ser medida, porque o branco não revela o que o modelo responderia.

## Análises

- **Gabarito definitivo:** as primeiras versões do artigo incluíam as questões com gabarito preliminar na análise principal. Em 07/11/2026, depois de comentário do orientador, a análise principal passou a seguir a regra registrada: 694 das 800 questões. A base com todas as questões ficou como sensibilidade, e a leitura de H2 não mudou.
- **Regressões mistas:** previstas nas hipóteses, foram executadas depois da análise descritiva.
- **Proporção de erros com alta confiança**, análises por área, confiança no ENEM e comparação com usuários são exploratórias, fora das hipóteses H1 a H6.

## Coleta e amostras

- O piloto de 60 questões rodou antes do registro das hipóteses e não faz parte das amostras do artigo.
- O sorteio se limitou às questões já classificadas; 2.993 elegíveis ficaram fora.
- O Gemini parou em 194 questões de certo/errado, antes do corte para gabarito definitivo.
- O contexto do Qwen 3 caiu de 16 mil para 8 mil tokens depois de 232 respostas do núcleo, e o Gemma 2 passou a 4 mil em parte da extensão.
- Temperatura 0 e semente fixa não garantiram resultado idêntico ao repetir questões.

## Questões

- Questões com imagem ficaram fora.
- Ao menos 9 das 694 questões dependiam de texto ou dado ausente e permaneceram nos cálculos.
- Seis questões têm alternativa vazia ou com texto de rodapé e permaneceram nos cálculos.
- As sensibilidades estão em `resultados/resultados.json`.

## Alcance

- Foram avaliados modelos locais e acessíveis por APIs gratuitas, sem versões pagas de ponta nem aplicativos.
- A confiança é um número pedido ao modelo e não se generaliza para conversas comuns.
- Provas até 2024 podem ter entrado no treino dos modelos.
- O risco para candidatos motivou o estudo, mas não foi medido.
