"""
Robô do Xereta: coleta os dados da API do SAPL da Câmara Municipal de Campinas
e monta a página index.html.

Uso:
  python coletar.py                 coleta tudo na API e gera index.html
  python coletar.py --bruto x.json  gera index.html a partir de uma coleta salva
"""
import base64, collections, io, json, sys, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

import requests

API = 'https://sapl.campinas.sp.leg.br/api'
FUSO = timezone(timedelta(hours=-3))
HOJE = datetime.now(FUSO).strftime('%Y-%m-%d')
sessao = requests.Session()
sessao.headers['User-Agent'] = 'Xereta (acompanhamento cidadao da Camara de Campinas)'


# ---------------------------------------------------------------- acesso à API
def get(caminho):
    """Busca um endereço da API com até 5 tentativas."""
    url = caminho if caminho.startswith('http') else API + caminho
    for t in range(5):
        try:
            r = sessao.get(url, timeout=60)
            if r.status_code == 404:
                return None
            if r.ok:
                return r.json()
        except requests.RequestException:
            pass
        time.sleep(2 * (t + 1))
    raise RuntimeError('Falha ao acessar ' + url)


def todos(ep, filtros=''):
    """Busca todas as páginas de uma lista, sempre ordenada por id (o=id).
    Sem o=id a API devolve linhas repetidas e pula outras."""
    base = f'{ep}?{filtros}&page_size=100&o=id'
    primeira = get(base + '&page=1')
    paginas = primeira['pagination']['total_pages']
    resto = paralelo(lambda p: get(f'{base}&page={p}')['results'], range(2, paginas + 1), 4)
    return primeira['results'] + [x for pg in resto for x in pg]


def paralelo(fn, itens, n=6):
    with ThreadPoolExecutor(n) as ex:
        return list(ex.map(fn, list(itens)))


def blocos(lista, n):
    lista = list(lista)
    return [lista[i:i + n] for i in range(0, len(lista), n)]


def log(msg):
    print(datetime.now(FUSO).strftime('%H:%M:%S'), msg, flush=True)


# ---------------------------------------------------------------- coleta
def coletar():
    R = {'gerado_em': datetime.now(timezone.utc).isoformat()}

    log('legislatura e parlamentares')
    legs = todos('/parlamentares/legislatura/')
    leg = next(l for l in legs if l['data_inicio'] <= HOJE <= l['data_fim'])
    INICIO = leg['data_inicio']
    R['legislatura'] = {'id': leg['id'], 'numero': leg['numero'], 'inicio': INICIO, 'fim': leg['data_fim']}
    R['parlamentares'] = [dict(id=x['id'], nome=x['nome_parlamentar'], completo=x['nome_completo'], ativo=x['ativo'],
                               email=x.get('email') or None, tel=x.get('telefone') or None, prof=x.get('profissao') or None,
                               foto=x.get('fotografia') or None)
                          for x in todos('/parlamentares/parlamentar/')]
    R['partidos'] = [dict(id=x['id'], sigla=x['sigla']) for x in todos('/parlamentares/partido/')]
    R['filiacoes'] = [dict(p=x['parlamentar'], partido=x['partido'], ini=x['data'], fim=x['data_desfiliacao'])
                      for x in todos('/parlamentares/filiacao/')]
    R['mandatos'] = [dict(p=x['parlamentar'], leg=x['legislatura'], ini=x['data_inicio_mandato'], fim=x['data_fim_mandato'],
                          titular=x['titular']) for x in todos('/parlamentares/mandato/', f'legislatura={leg["id"]}')]

    log('sessões')
    lista = get('/sessao/sessaoplenaria/')
    R['sessoes'] = [dict(id=s['id'], n=s['numero'], d=s['data_inicio'], t=s['tipo'], ata=s.get('upload_ata') or None,
                         pauta=s.get('upload_pauta') or None) for s in lista if s['data_inicio'] >= INICIO]

    log('comissões')
    R['comissoes'] = [dict(id=x['id'], nome=x['nome'], sigla=x['sigla'], tipo=x['tipo']) for x in todos('/comissoes/comissao/')]
    R['cargos'] = {x['id']: x['nome'] for x in todos('/comissoes/cargocomissao/')}
    periodo = next((p for p in todos('/comissoes/periodo/') if p['data_inicio'] <= HOJE <= (p['data_fim'] or '9999')), None)
    comps = todos('/comissoes/composicao/', f'periodo={periodo["id"]}') if periodo else []
    R['composicoes'] = [dict(id=c['id'], comissao=c['comissao']) for c in comps]
    parts = paralelo(lambda c: todos('/comissoes/participacao/', f'composicao={c["id"]}'), comps, 4)
    R['participacoes'] = [dict(comp=c['id'], p=x['parlamentar'], cargo=x['cargo'], titular=x['titular'], fim=x['data_desligamento'])
                          for c, ps in zip(comps, parts) for x in ps]

    log('pauta das sessões')
    od = todos('/sessao/ordemdia/', f'data_ordem__gte={INICIO}')
    ex = todos('/sessao/expedientemateria/', f'data_ordem__gte={INICIO}')
    item = {('OD', x['id']): x for x in od}
    item.update({('EX', x['id']): x for x in ex})

    log('votações')
    def rv(campo, ids):
        return todos('/sessao/registrovotacao/', f'{campo}__in=' + ','.join(map(str, ids)))
    regs = [x for b in paralelo(lambda b: rv('ordem', b), blocos([x['id'] for x in od], 80), 4) for x in b]
    regs += [x for b in paralelo(lambda b: rv('expediente', b), blocos([x['id'] for x in ex], 80), 4) for x in b]
    vistos, R['votacoes'] = set(), []
    for v in regs:
        if v['id'] in vistos:
            continue
        vistos.add(v['id'])
        fase = 'OD' if v['ordem'] else 'EX'
        it = item.get((fase, v['ordem'] or v['expediente']))
        if not it:
            continue
        rot = (v['__str__'].split(' - Matéria: ')[1:] or [''])[0].strip()
        R['votacoes'].append(dict(id=v['id'], m=v['materia'], s=it['sessao_plenaria'], fase=fase, turno=it.get('turno'),
                                  res=v['tipo_resultado_votacao'], mat=rot))

    log(f'votos individuais de {len(R["votacoes"])} votações')
    vids = [v['id'] for v in R['votacoes']]
    votos = paralelo(lambda b: todos('/sessao/votoparlamentar/', 'votacao__in=' + ','.join(map(str, b))), blocos(vids, 40), 4)
    R['votos'] = [[x['votacao'], x['parlamentar'], normaliza(x['voto'])] for b in votos for x in b]

    log('presença')
    sids = [s['id'] for s in R['sessoes'] if s['d'] <= HOJE]
    pres = paralelo(lambda b: todos('/sessao/sessaoplenariapresenca/', 'sessao_plenaria__in=' + ','.join(map(str, b))), blocos(sids, 40), 4)
    R['presenca'] = [[x['sessao_plenaria'], x['parlamentar']] for b in pres for x in b]

    log('reuniões e relatorias das comissões')
    R['reunioes'] = [dict(id=x['id'], c=x['comissao'], n=x['numero'], nome=x['nome'], tema=x['tema'], d=x['data'],
                          hi=x['hora_inicio'], hf=x['hora_fim'], local=x['local_reuniao'], obs=x['observacao'],
                          video=x.get('url_video') or None, audio=x.get('url_audio') or None, pauta=x.get('upload_pauta') or None,
                          ata=x.get('upload_ata') or None, anexo=x.get('upload_anexo') or None)
                     for x in todos('/comissoes/reuniao/', f'data__gte={INICIO}')]
    R['relatorias'] = [dict(m=x['materia'], p=x['parlamentar'], c=x['comissao'], ini=x['data_designacao_relator'],
                            fim=x['data_destituicao_relator'], tfim=x['tipo_fim_relatoria'])
                       for x in todos('/materia/relatoria/', f'data_designacao_relator__gte={INICIO}')]
    R['tipos_fim'] = {x['id']: x.get('descricao') or x['__str__'] for x in todos('/materia/tipofimrelatoria/')}

    log('matérias')
    R['tipos_materia'] = {x['id']: dict(sigla=x['sigla'], desc=x['descricao']) for x in todos('/materia/tipomaterialegislativa/')}
    nominais = {v for v, p, c in R['votos'] if c in 'SNA'}
    mats = {v['m'] for v in R['votacoes'] if v['id'] in nominais} | {r['m'] for r in R['relatorias']}
    cache = carregar_cache()
    faltam = [m for m in mats if str(m) not in cache]
    log(f'{len(mats)} matérias, {len(faltam)} novas')
    for m, x in zip(faltam, paralelo(lambda m: get(f'/materia/materialegislativa/{m}/'), faltam, 6)):
        if x:
            cache[str(m)] = dict(tipo=x['tipo'], n=x['numero'], ano=x['ano'], ementa=x['ementa'],
                                 autores=x['autores'], texto=x.get('texto_original') or None)
    autores = {a for m in mats if str(m) in cache for a in cache[str(m)]['autores']}
    faltam = [a for a in autores if 'a' + str(a) not in cache]
    for a, x in zip(faltam, paralelo(lambda a: get(f'/base/autor/{a}/'), faltam, 6)):
        if x:
            cache['a' + str(a)] = x['nome']
    salvar_cache(cache)
    R['materias'] = {m: cache[str(m)] for m in mats if str(m) in cache}
    R['autores'] = {a: cache.get('a' + str(a), '') for a in autores}

    log('fotos')
    pessoas = {m['p'] for m in R['mandatos']} | {p for _, p, _ in R['votos']}
    fotos = paralelo(lambda p: miniatura(next((x['foto'] for x in R['parlamentares'] if x['id'] == p), None)), pessoas, 4)
    R['fotos'] = {p: f for p, f in zip(pessoas, fotos) if f}
    return R


def normaliza(s):
    s = (s or '').strip()
    if s == 'Sim': return 'S'
    if s in ('Não', 'Nao'): return 'N'
    if s == 'Abstenção': return 'A'
    if s in ('Presidente não vota', 'Na Presid.'): return 'P'
    return 'F'  # Ausente, Ausente na Votação, -1, Não Votou, vazio e outros


def miniatura(url):
    if not url:
        return None
    try:
        from PIL import Image
        r = sessao.get(url, timeout=60)
        r.raise_for_status()
        im = Image.open(io.BytesIO(r.content)).convert('RGB')
        s = min(im.size)
        x = (im.width - s) // 2
        y = int(max(0, (im.height - s) * 0.2))
        im = im.crop((x, y, x + s, y + s)).resize((160, 160), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, 'JPEG', quality=80)
        return 'data:image/jpeg;base64,' + base64.b64encode(buf.getvalue()).decode()
    except Exception as e:
        log(f'foto falhou: {url} ({e})')
        return None


def carregar_cache():
    try:
        return json.load(open('cache_materias.json', encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def salvar_cache(c):
    json.dump(c, open('cache_materias.json', 'w', encoding='utf-8'), ensure_ascii=False, separators=(',', ':'))


# ---------------------------------------------------------------- montagem
RESULTADOS = {1: 'Aprovado', 2: 'Rejeitado', 3: 'Não votado', 4: 'Mantido', 7: 'Acatado', 8: 'Procedente', 9: 'Improcedente'}


def categoria(rot):
    r = rot.lower()
    if r.startswith('requerimento'): return 'req'
    if r.startswith('moção'): return 'moc'
    if r.startswith('ata'): return 'ata'
    if r.startswith(('projeto', 'emenda', 'substitutivo', 'recurso', 'proposta')): return 'proj'
    return 'out'


def montar(R):
    hoje = R.get('hoje') or HOJE
    sess = {s['id']: s for s in R['sessoes']}
    parl = {p['id']: p for p in R['parlamentares']}
    sigla_partido = {p['id']: p['sigla'] for p in R['partidos']}
    fil = collections.defaultdict(list)
    for f in R['filiacoes']:
        fil[f['p']].append(f)

    def partido(pid):
        vig = [f for f in fil.get(pid, []) if f['ini'] and f['ini'] <= hoje and (not f['fim'] or f['fim'] >= hoje)]
        f = max(vig or fil.get(pid, [{'ini': ''}]), key=lambda f: f['ini'] or '')
        return sigla_partido.get(f.get('partido'))

    mand = {}
    for m in R['mandatos']:
        if m['p'] not in mand or (m['ini'] or '') < (mand[m['p']]['ini'] or ''):
            mand[m['p']] = m
    votos_por = collections.defaultdict(dict)
    for v, p, c in R['votos']:
        votos_por[v][p] = c
    nominais = {v for v, d in votos_por.items() if any(c in 'SNA' for c in d.values())}
    pres = collections.defaultdict(set)
    for s, p in R['presenca']:
        pres[s].add(p)
    comp = {c['id']: c['comissao'] for c in R['composicoes']}
    cargos = {int(k): v for k, v in R['cargos'].items()}

    pessoas = set(mand) | {p for d in votos_por.values() for p in d}
    V = []
    for pid in pessoas:
        p = parl.get(pid)
        if not p:
            continue
        m = mand.get(pid)
        cnt, alin = collections.Counter(), [0, 0]
        for vid in nominais:
            c = votos_por[vid].get(pid)
            if not c:
                continue
            cnt[c] += 1
            s_ = sum(1 for x in votos_por[vid].values() if x == 'S')
            n_ = sum(1 for x in votos_por[vid].values() if x == 'N')
            if c in 'SN' and s_ != n_:
                alin[1] += 1
                alin[0] += (c == 'S') == (s_ > n_)
        com = [[comp[x['comp']], cargos.get(x['cargo'], ''), x['titular']] for x in R['participacoes']
               if x['p'] == pid and not x['fim'] and x['comp'] in comp]
        V.append(dict(id=pid, nome=p['nome'], completo=p['completo'], ativo=p['ativo'], partido=partido(pid),
                      email=p['email'], tel=p['tel'], prof=p['prof'],
                      mand=[m['ini'], m['fim'], bool(m['titular'])] if m else None,
                      votos=dict(cnt), alin=alin, com=com))
    V.sort(key=lambda x: x['nome'])

    VT = []
    for v in R['votacoes']:
        if v['s'] not in sess:
            continue
        VT.append([v['id'], v['m'], v['s'], v['fase'], v['res'], 0, 0, 0, v['mat'], categoria(v['mat']), v['turno']])
    VT.sort(key=lambda r: (sess[r[2]]['d'], r[0]), reverse=True)

    tipos = {int(k): v for k, v in R['tipos_materia'].items()}
    autores = {int(k): v for k, v in R['autores'].items()}
    MAT = {}
    for k, m in R['materias'].items():
        MAT[str(k)] = [tipos.get(m['tipo'], {}).get('sigla'), m['n'], m['ano'], m['ementa'],
                       [autores.get(a, '') for a in (m['autores'] or [])], m['texto']]
    usadas = {c[0] for v in V for c in v['com']} | {r['c'] for r in R['reunioes']} | {r['c'] for r in R['relatorias']}
    COM = {str(c['id']): [c['nome'], c['sigla'], c['tipo']] for c in R['comissoes'] if c['id'] in usadas}

    dados = dict(
        gerado=R['gerado_em'], hoje=hoje, res={str(k): v for k, v in RESULTADOS.items()},
        V=V, S={s['id']: [s['n'], s['d'], s['t'], s['ata'], s['pauta']] for s in R['sessoes']},
        VT=VT, NOM={vid: ','.join(f'{p}{c}' for p, c in votos_por[vid].items()) for vid in nominais},
        MAT=MAT, COM=COM,
        PRES={s: ','.join(map(str, sorted(ps))) for s, ps in pres.items()},
        TIPOS={t['sigla']: t['desc'] for t in tipos.values()},
        FOTOS={str(k): v for k, v in R['fotos'].items()},
        REU=[[r['id'], r['c'], r['n'], r['nome'], r['tema'], r['d'], r['hi'], r['hf'], r['local'], r['obs'],
              r['video'], r['audio'], r['pauta'], r['ata'], r['anexo']] for r in R['reunioes']],
        REL=[[r['m'], r['p'], r['c'], r['ini'], r['fim'], None, r['tfim']] for r in R['relatorias'] if r['ini']],
        TFIM={str(k): v for k, v in R['tipos_fim'].items()},
    )
    js = json.dumps(dados, ensure_ascii=False, separators=(',', ':')).replace('</', '<\\/')
    html = open('modelo.html', encoding='utf-8').read().replace('__DATA__', js)
    open('index.html', 'w', encoding='utf-8').write(html)
    log(f'index.html gerado: {len(html)//1024} KB, {len(VT)} votações, {len(nominais)} nominais, '
        f'{len(R["reunioes"])} reuniões, {len(dados["FOTOS"])} fotos')
    return dados


if __name__ == '__main__':
    if '--bruto' in sys.argv:
        R = json.load(open(sys.argv[sys.argv.index('--bruto') + 1], encoding='utf-8'))
    else:
        R = coletar()
        json.dump(R, open('coleta.json', 'w', encoding='utf-8'), ensure_ascii=False)
    d = montar(R)
    if len(d['VT']) < 100 or len(d['V']) < 20:
        sys.exit('Coleta incompleta: index.html não deve ser publicado.')
