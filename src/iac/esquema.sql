-- Esquema do banco do projeto (versão 2, reescrita após a revisão de 15/10/2026).
-- Regras gerais:
--   * texto original preservado; colunas de hash/normalização só para casar e deduplicar;
--   * ids de `questao` são ESTÁVEIS (upsert por hash_conteudo), porque amostra e execução apontam para eles;
--   * toda exclusão vira linha em `exclusao` (fluxograma de exclusões do artigo);
--   * datas em ISO 8601: 'AAAA-MM-DD' (dia) ou 'AAAA-MM' (mês), validadas por CHECK.
-- Mudanças depois que houver execuções: migrações aplicadas por db.py; nunca editar isto de forma destrutiva.

-- ------------------------------------------------------------------ domínios

CREATE TABLE IF NOT EXISTS disciplina (
    slug       TEXT PRIMARY KEY,
    macroarea  TEXT NOT NULL CHECK (macroarea IN ('portugues','linguas','matematica','direito','ti','gestao','outras'))
);

-- ------------------------------------------------------------------ corpus

CREATE TABLE IF NOT EXISTS banca (
    id     INTEGER PRIMARY KEY,
    sigla  TEXT NOT NULL UNIQUE,
    nome   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS concurso (
    id                   INTEGER PRIMARY KEY,
    banca_id             INTEGER NOT NULL REFERENCES banca(id),
    slug                 TEXT NOT NULL,
    titulo               TEXT,
    orgao                TEXT,
    esfera               TEXT CHECK (esfera IN ('federal','estadual','municipal','distrital')),
    poder                TEXT CHECK (poder IN ('executivo','legislativo','judiciario','mp','defensoria','tribunal_contas','estatal','outro')),
    uf                   TEXT CHECK (uf IS NULL OR length(uf) = 2),
    regiao               TEXT CHECK (regiao IN ('N','NE','CO','SE','S','nacional')),
    ano_edital           INTEGER CHECK (ano_edital IS NULL OR ano_edital BETWEEN 2000 AND 2100),
    data_prova           TEXT CHECK (data_prova IS NULL OR data_prova GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    data_prova_fonte     TEXT CHECK (data_prova_fonte IN ('gabarito','edital','manual','estimada_ano')),
    data_gab_preliminar  TEXT CHECK (data_gab_preliminar IS NULL OR data_gab_preliminar GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    data_gab_definitivo  TEXT CHECK (data_gab_definitivo IS NULL OR data_gab_definitivo GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    url_fonte            TEXT,
    UNIQUE (banca_id, slug)
);
CREATE INDEX IF NOT EXISTS ix_concurso_banca ON concurso(banca_id);

CREATE TABLE IF NOT EXISTS documento (
    id            INTEGER PRIMARY KEY,
    concurso_id   INTEGER NOT NULL REFERENCES concurso(id),
    url           TEXT NOT NULL UNIQUE,
    nome_arquivo  TEXT NOT NULL,
    tipo          TEXT NOT NULL CHECK (tipo IN
                  ('prova','gabarito_preliminar','gabarito_definitivo','gabarito_retificado','edital','outro')),
    sha256        TEXT,
    bytes         INTEGER,
    baixado_em    TEXT,
    erro          TEXT
);
CREATE INDEX IF NOT EXISTS ix_documento_concurso ON documento(concurso_id, tipo);
CREATE INDEX IF NOT EXISTS ix_documento_sha ON documento(sha256);

CREATE TABLE IF NOT EXISTS cargo (
    id          INTEGER PRIMARY KEY,
    nome_norm   TEXT NOT NULL UNIQUE,
    nome        TEXT NOT NULL,
    area        TEXT CHECK (area IN ('ti','administrativa','juridica','fiscal_controle','saude','educacao',
                                     'seguranca','engenharia','legislativa','outra')),
    area_fonte  TEXT CHECK (area_fonte IN ('regra','llm','humano'))
);

CREATE TABLE IF NOT EXISTS caderno (
    id               INTEGER PRIMARY KEY,
    documento_id     INTEGER NOT NULL UNIQUE REFERENCES documento(id),
    concurso_id      INTEGER NOT NULL REFERENCES concurso(id),
    cargo_id         INTEGER REFERENCES cargo(id),
    nivel            TEXT CHECK (nivel IN ('fundamental','medio','superior')),
    tipo_caderno     TEXT,
    formato          TEXT CHECK (formato IN ('ME5','ME4','CE')),
    n_itens          INTEGER NOT NULL,
    n_esperado       INTEGER,                 -- de fonte independente da segmentação
    fonte_esperado   TEXT CHECK (fonte_esperado IN ('capa','gabarito','sequencia')),
    status           TEXT NOT NULL CHECK (status IN ('ok','quarentena')),
    gabarito_status  TEXT CHECK (gabarito_status IN ('casado','sem_tabela','ambiguo','duvidoso','tamanho_diferente')),
    gab_prelim_doc   INTEGER REFERENCES documento(id),   -- de onde veio cada gabarito (auditoria)
    gab_defin_doc    INTEGER REFERENCES documento(id),
    versao_extrator  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_caderno_concurso ON caderno(concurso_id, status);
CREATE INDEX IF NOT EXISTS ix_caderno_cargo ON caderno(cargo_id);

CREATE TABLE IF NOT EXISTS texto_base (
    id          INTEGER PRIMARY KEY,
    caderno_id  INTEGER NOT NULL REFERENCES caderno(id),
    texto       TEXT NOT NULL,
    hash        TEXT NOT NULL,
    UNIQUE (caderno_id, hash)
);

CREATE TABLE IF NOT EXISTS item (
    id                   INTEGER PRIMARY KEY,
    caderno_id           INTEGER NOT NULL REFERENCES caderno(id),
    numero               INTEGER NOT NULL,
    secao                TEXT,
    texto_base_id        INTEGER REFERENCES texto_base(id),
    enunciado            TEXT NOT NULL,
    alternativas_json    TEXT CHECK (alternativas_json IS NULL OR json_valid(alternativas_json)),
    tem_imagem           INTEGER NOT NULL DEFAULT 0 CHECK (tem_imagem IN (0,1)),
    hash_conteudo        TEXT NOT NULL,
    gabarito_preliminar  TEXT CHECK (gabarito_preliminar IN ('A','B','C','D','E','X')),
    gabarito_definitivo  TEXT CHECK (gabarito_definitivo IN ('A','B','C','D','E','X')),
    questao_id           INTEGER REFERENCES questao(id),
    UNIQUE (caderno_id, numero)
);
CREATE INDEX IF NOT EXISTS ix_item_questao ON item(questao_id);
CREATE INDEX IF NOT EXISTS ix_item_hash ON item(hash_conteudo);

-- Questão canônica: id estável, upsert por hash_conteudo.
CREATE TABLE IF NOT EXISTS questao (
    id                     INTEGER PRIMARY KEY,
    hash_conteudo          TEXT NOT NULL UNIQUE,
    item_canonico_id       INTEGER NOT NULL REFERENCES item(id),
    formato                TEXT NOT NULL CHECK (formato IN ('ME5','ME4','CE')),
    gabarito               TEXT,
    gabarito_fonte         TEXT CHECK (gabarito_fonte IN ('definitivo','preliminar')),
    alterada_recurso       INTEGER CHECK (alterada_recurso IN (0,1)),
    disciplina             TEXT REFERENCES disciplina(slug),
    disciplina_fonte       TEXT CHECK (disciplina_fonte IN ('secao','llm','humano')),
    elegivel               INTEGER NOT NULL DEFAULT 0 CHECK (elegivel IN (0,1)),
    -- covariáveis derivadas (congeladas junto com o núcleo)
    nivel_questao          TEXT,           -- nível único ou 'misto'
    area_questao           TEXT,           -- área única ou 'multipla'
    n_cargos               INTEGER,
    n_concursos            INTEGER,
    data_primeira_prova    TEXT,           -- menor data_prova entre as ocorrências (P7)
    n_carac_enunciado      INTEGER,
    n_carac_texto_base     INTEGER NOT NULL DEFAULT 0,
    n_alternativas         INTEGER,
    tem_texto_base         INTEGER NOT NULL DEFAULT 0 CHECK (tem_texto_base IN (0,1)),
    tem_destaque           INTEGER NOT NULL DEFAULT 0 CHECK (tem_destaque IN (0,1)),
    comando_negativo       INTEGER NOT NULL DEFAULT 0 CHECK (comando_negativo IN (0,1)),  -- EXCETO, INCORRETA, NÃO
    cita_norma             INTEGER NOT NULL DEFAULT 0 CHECK (cita_norma IN (0,1)),        -- Lei nº, art., Súmula
    tem_codigo             INTEGER NOT NULL DEFAULT 0 CHECK (tem_codigo IN (0,1)),
    tem_afirmativas        INTEGER NOT NULL DEFAULT 0 CHECK (tem_afirmativas IN (0,1)),   -- I, II, III
    permutavel             INTEGER NOT NULL DEFAULT 1 CHECK (permutavel IN (0,1)),        -- sem "todas as anteriores"
    CHECK (formato = 'CE' OR gabarito IS NULL OR gabarito IN ('A','B','C','D','E','X')),
    CHECK (formato <> 'CE' OR gabarito IS NULL OR gabarito IN ('C','E','X')),
    CHECK (elegivel = 0 OR (gabarito IS NOT NULL AND gabarito <> 'X'))
);
CREATE INDEX IF NOT EXISTS ix_questao_elegivel ON questao(elegivel, formato);

-- Uma linha por motivo de exclusão (formato longo): várias razões por entidade, por etapa.
CREATE TABLE IF NOT EXISTS exclusao (
    id            INTEGER PRIMARY KEY,
    etapa         TEXT NOT NULL CHECK (etapa IN ('baixar','extrair','gabaritar','consolidar','classificar','amostrar','executar','avaliar')),
    entidade      TEXT NOT NULL CHECK (entidade IN ('documento','caderno','item','questao','execucao')),
    entidade_id   INTEGER NOT NULL,
    motivo        TEXT NOT NULL,
    detalhe       TEXT,
    regra_versao  TEXT NOT NULL,
    criada_em     TEXT NOT NULL,
    UNIQUE (etapa, entidade, entidade_id, motivo)
);
CREATE INDEX IF NOT EXISTS ix_exclusao_motivo ON exclusao(etapa, motivo);

-- ------------------------------------------------------------------ experimento

CREATE TABLE IF NOT EXISTS modelo (
    id                    INTEGER PRIMARY KEY,
    slug                  TEXT NOT NULL UNIQUE,
    familia               TEXT NOT NULL,
    fornecedor            TEXT NOT NULL,
    pesos                 TEXT NOT NULL CHECK (pesos IN ('aberto','fechado')),
    parametros_b          REAL,
    parametros_ativos_b   REAL,                 -- MoE: ativos por token
    data_lancamento       TEXT CHECK (data_lancamento IS NULL OR data_lancamento GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]*'),
    corte_conhecimento    TEXT CHECK (corte_conhecimento IS NULL OR corte_conhecimento GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]'),
    corte_fonte           TEXT CHECK (corte_fonte IN ('oficial','estimado')),
    acesso_consumidor     TEXT CHECK (acesso_consumidor IN ('gratuito','pago','nenhum')),
    acesso_verificado_em  TEXT,
    via                   TEXT NOT NULL,
    nome_api              TEXT NOT NULL,
    versao                TEXT,                 -- snapshot/tag exata
    quantizacao           TEXT,                 -- 'Q4_K_M' etc.; NULL em API
    digest                TEXT,                 -- digest do pacote local (Ollama)
    contexto_max          INTEGER,
    raciocinio            INTEGER NOT NULL DEFAULT 0 CHECK (raciocinio IN (0,1)),
    treina_com_dados      INTEGER CHECK (treina_com_dados IN (0,1)),   -- política da via
    preco_in_usd_mtok     REAL,
    preco_out_usd_mtok    REAL,
    CHECK ((preco_in_usd_mtok IS NULL) = (preco_out_usd_mtok IS NULL))
);

CREATE TABLE IF NOT EXISTS condicao (
    id           INTEGER PRIMARY KEY,
    slug         TEXT NOT NULL UNIQUE,
    formato      TEXT NOT NULL CHECK (formato IN ('ME','CE')),
    temperatura  REAL NOT NULL CHECK (temperatura BETWEEN 0 AND 2),
    repeticoes   INTEGER NOT NULL CHECK (repeticoes >= 1),
    embaralhar   INTEGER NOT NULL CHECK (embaralhar IN (0,1)),
    json_mode    INTEGER NOT NULL DEFAULT 0 CHECK (json_mode IN (0,1)),
    template     TEXT NOT NULL,
    config_sha   TEXT NOT NULL              -- sha de TODOS os campos acima
);

CREATE TABLE IF NOT EXISTS amostra (
    id             INTEGER PRIMARY KEY,
    nome           TEXT NOT NULL UNIQUE,
    semente        INTEGER NOT NULL,
    criterio       TEXT NOT NULL,
    n_pedido       INTEGER NOT NULL,
    versao_codigo  TEXT,
    criada_em      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS amostra_estrato (
    amostra_id    INTEGER NOT NULL REFERENCES amostra(id),
    estrato       TEXT NOT NULL,
    n_populacao   INTEGER NOT NULL CHECK (n_populacao >= 1),
    n_amostra     INTEGER NOT NULL CHECK (n_amostra >= 1),
    PRIMARY KEY (amostra_id, estrato)
);

CREATE TABLE IF NOT EXISTS amostra_questao (
    amostra_id  INTEGER NOT NULL,
    questao_id  INTEGER NOT NULL REFERENCES questao(id),
    estrato     TEXT NOT NULL,
    PRIMARY KEY (amostra_id, questao_id),
    FOREIGN KEY (amostra_id, estrato) REFERENCES amostra_estrato(amostra_id, estrato)
);
CREATE INDEX IF NOT EXISTS ix_amq_questao ON amostra_questao(questao_id);

CREATE TABLE IF NOT EXISTS execucao (
    id                  INTEGER PRIMARY KEY,
    chave               TEXT NOT NULL UNIQUE,   -- modelo|condicao@config_sha|questao|repeticao
    modelo_id           INTEGER NOT NULL REFERENCES modelo(id),
    condicao_id         INTEGER NOT NULL REFERENCES condicao(id),
    questao_id          INTEGER NOT NULL REFERENCES questao(id),
    repeticao           INTEGER NOT NULL,
    permutacao_json     TEXT CHECK (permutacao_json IS NULL OR json_valid(permutacao_json)),
    prompt_sha          TEXT NOT NULL,
    parametros_json     TEXT NOT NULL,          -- corpo da requisição SEM cabeçalhos e sem o prompt
    status              TEXT NOT NULL CHECK (status IN ('reservada','ok','erro')),
    resposta_bruta      TEXT,                   -- conteúdo da mensagem
    raciocinio_texto    TEXT,                   -- "pensamento" exposto pela via, quando houver
    logprobs_json       TEXT,                   -- probabilidade por letra, quando a via fornecer
    finish_reason       TEXT,                   -- 'stop' | 'length' | ...
    modelo_retornado    TEXT,
    provedor_real       TEXT,                   -- quem serviu (OpenRouter roteia)
    system_fingerprint  TEXT,
    erro                TEXT,
    latencia_ms         INTEGER,
    tokens_in           INTEGER,
    tokens_out          INTEGER,
    tokens_raciocinio   INTEGER,
    tentativas          INTEGER NOT NULL DEFAULT 1,
    custo_usd           REAL NOT NULL DEFAULT 0,  -- reservado (pior caso) enquanto status='reservada'
    primeira_em         TEXT NOT NULL,
    criada_em           TEXT NOT NULL,
    UNIQUE (modelo_id, condicao_id, questao_id, repeticao)
);
CREATE INDEX IF NOT EXISTS ix_execucao_questao ON execucao(questao_id);
CREATE INDEX IF NOT EXISTS ix_execucao_criada ON execucao(criada_em);
CREATE INDEX IF NOT EXISTS ix_execucao_status ON execucao(status, modelo_id);

CREATE TABLE IF NOT EXISTS avaliacao (
    execucao_id       INTEGER PRIMARY KEY REFERENCES execucao(id),
    formato_ok        INTEGER NOT NULL CHECK (formato_ok IN (0,1)),
    metodo_extracao   TEXT CHECK (metodo_extracao IN ('json','json_reparado','regex')),
    motivo_invalido   TEXT,                    -- 'truncada', 'sem_letra', 'ambigua', 'fora_do_mapa'...
    resposta_exibida  TEXT,
    resposta          TEXT,
    absteve           INTEGER NOT NULL DEFAULT 0 CHECK (absteve IN (0,1)),
    confianca_bruta   TEXT,
    confianca         REAL CHECK (confianca IS NULL OR confianca BETWEEN 0 AND 100),
    prob_resposta     REAL,                    -- probabilidade interna da letra escolhida (logprobs)
    justificativa     TEXT,
    acertou           INTEGER CHECK (acertou IN (0,1)),
    pontos            REAL,
    versao_avaliador  TEXT NOT NULL,
    CHECK (formato_ok = 1 OR (resposta IS NULL AND acertou IS NULL)),
    CHECK (absteve = 0 OR acertou IS NULL)
);

-- Delírio e categoria de erro, por juiz (LLM versionado ou humano às cegas).
CREATE TABLE IF NOT EXISTS julgamento (
    execucao_id          INTEGER NOT NULL REFERENCES execucao(id),
    juiz                 TEXT NOT NULL,         -- 'llm:<slug>@<sha do prompt>' | 'humano:<anotador>'
    contradicao          INTEGER CHECK (contradicao IN (0,1)),
    premissa_falsa       INTEGER CHECK (premissa_falsa IN (0,1)),
    categoria_erro       TEXT CHECK (categoria_erro IN ('lei_desatualizada','ignorou_comando','calculo',
                                     'interpretacao','pegadinha','norma_inventada','confusao_alternativas',
                                     'conhecimento_ausente','gabarito_suspeito','outro')),
    evidencia            TEXT,                  -- trecho que sustenta o rótulo
    cego                 INTEGER NOT NULL DEFAULT 1 CHECK (cego IN (0,1)),
    criado_em            TEXT NOT NULL,
    PRIMARY KEY (execucao_id, juiz)
);

-- Citações normativas extraídas da justificativa, verificadas de forma determinística.
CREATE TABLE IF NOT EXISTS citacao (
    id           INTEGER PRIMARY KEY,
    execucao_id  INTEGER NOT NULL REFERENCES execucao(id),
    tipo         TEXT NOT NULL CHECK (tipo IN ('lei','lei_complementar','decreto','sumula','sumula_vinculante','artigo','outro')),
    numero       TEXT,
    ano          INTEGER,
    trecho       TEXT NOT NULL,
    existe       INTEGER CHECK (existe IN (0,1)),   -- NULL = não verificável
    fonte        TEXT
);
CREATE INDEX IF NOT EXISTS ix_citacao_execucao ON citacao(execucao_id);

-- ------------------------------------------------------------------ visões

CREATE VIEW IF NOT EXISTS v_questao_cargo AS
SELECT DISTINCT i.questao_id, ca.concurso_id, ca.cargo_id, ca.nivel, cg.area
FROM item i JOIN caderno ca ON ca.id = i.caderno_id LEFT JOIN cargo cg ON cg.id = ca.cargo_id
WHERE i.questao_id IS NOT NULL AND ca.status = 'ok';

-- Fluxograma de exclusões (CONSORT)
CREATE VIEW IF NOT EXISTS v_funil AS
SELECT 1 AS ordem, 'documentos de prova' AS etapa, COUNT(*) AS n FROM documento WHERE tipo = 'prova'
UNION ALL SELECT 2, 'cadernos extraídos', COUNT(*) FROM caderno
UNION ALL SELECT 3, 'cadernos ok', COUNT(*) FROM caderno WHERE status = 'ok'
UNION ALL SELECT 4, 'cadernos com gabarito', COUNT(*) FROM caderno WHERE status = 'ok' AND gabarito_status = 'casado'
UNION ALL SELECT 5, 'itens em cadernos ok', COUNT(*) FROM item i JOIN caderno c ON c.id = i.caderno_id WHERE c.status = 'ok'
UNION ALL SELECT 6, 'questões únicas', COUNT(*) FROM questao
UNION ALL SELECT 7, 'questões elegíveis', COUNT(*) FROM questao WHERE elegivel = 1;

-- Detector de troca silenciosa de modelo por trás da API
CREATE VIEW IF NOT EXISTS v_deriva_modelo AS
SELECT m.slug, e.modelo_retornado, e.system_fingerprint, e.provedor_real,
       MIN(e.criada_em) AS de, MAX(e.criada_em) AS ate, COUNT(*) AS n
FROM execucao e JOIN modelo m ON m.id = e.modelo_id WHERE e.status = 'ok'
GROUP BY 1, 2, 3, 4;

-- Visão analítica principal: uma linha por execução (inclusive formato inválido), com todas as covariáveis.
CREATE VIEW IF NOT EXISTS v_resultado AS
SELECT e.id AS execucao_id, e.criada_em AS executada_em, m.slug AS modelo, m.familia, m.fornecedor, m.pesos,
       m.parametros_b, m.parametros_ativos_b, m.data_lancamento, m.corte_conhecimento, m.acesso_consumidor,
       m.via, m.raciocinio, m.quantizacao, e.modelo_retornado,
       c.slug AS condicao, c.temperatura, e.repeticao, e.permutacao_json IS NOT NULL AS permutada,
       q.id AS questao_id, q.formato, q.disciplina, d.macroarea, q.gabarito, q.gabarito_fonte, q.alterada_recurso,
       q.nivel_questao, q.area_questao, q.n_cargos, q.n_carac_enunciado, q.n_carac_texto_base, q.tem_texto_base,
       q.tem_destaque, q.comando_negativo, q.cita_norma, q.tem_codigo, q.tem_afirmativas,
       b.sigla AS banca, co.orgao, co.esfera, co.poder, co.uf, co.regiao, co.data_prova,
       CAST(substr(COALESCE(q.data_primeira_prova, co.data_prova), 1, 4) AS INTEGER) AS ano_prova,
       e.finish_reason, e.tokens_in, e.tokens_out, e.tokens_raciocinio, e.latencia_ms, e.custo_usd,
       a.formato_ok, a.motivo_invalido, a.resposta, a.absteve, a.confianca, a.prob_resposta, a.acertou, a.pontos,
       CASE WHEN a.formato_ok = 1 THEN a.acertou ELSE 0 END AS acertou_candidato,  -- inutilizável = erro
       CASE WHEN COALESCE(q.data_primeira_prova, co.data_prova) IS NULL OR m.corte_conhecimento IS NULL THEN NULL
            WHEN substr(COALESCE(q.data_primeira_prova, co.data_prova), 1, 7) = m.corte_conhecimento THEN NULL
            WHEN substr(COALESCE(q.data_primeira_prova, co.data_prova), 1, 7) > m.corte_conhecimento THEN 1
            ELSE 0 END AS pos_corte,
       CAST(julianday(COALESCE(q.data_primeira_prova, co.data_prova)) - julianday(m.corte_conhecimento || '-15')
            AS INTEGER) AS dias_apos_corte
FROM execucao e
JOIN avaliacao a ON a.execucao_id = e.id
JOIN modelo m    ON m.id = e.modelo_id
JOIN condicao c  ON c.id = e.condicao_id
JOIN questao q   ON q.id = e.questao_id
LEFT JOIN disciplina d ON d.slug = q.disciplina
JOIN item i      ON i.id = q.item_canonico_id
JOIN caderno ca  ON ca.id = i.caderno_id
JOIN concurso co ON co.id = ca.concurso_id
JOIN banca b     ON b.id = co.banca_id
WHERE e.status = 'ok';
