"""Deterministic B/Enseigne covers, gated by genuine final Reel feedback."""
import hashlib,json,re
from pathlib import Path
from datetime import datetime,UTC
from PIL import Image,ImageDraw,ImageFont,ImageOps
from app.services.acquisition_store import AcquisitionStore

def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()

COVER_STYLE={
    'version':'B-enseigne-fixed-type-v2',
    'canvas':[1080,1920], 'grid_crop':[0,240,1080,1680],
    'title_font':'/System/Library/Fonts/Supplemental/Impact.ttf', 'title_size':160,
    'title_center':[540,740], 'title_max_width':900, 'title_line_gap':20,
    'badge_font':'/System/Library/Fonts/Supplemental/Arial Bold.ttf', 'badge_size':50,
    'badge_bounds':[360,940,720,1046], 'badge_horizontal_padding':45,
    'text_color':'#fff9e9', 'badge_color':'#650a24', 'photo_darkening':.24,
    'stroke_width':0,
}


def title_lines(text,face,max_width):
    def width(line):
        box=face.getbbox(line,anchor='ls');return box[2]-box[0]
    if width(text)<=max_width:return [text]
    words=text.split();candidates=[]
    for split in range(1,len(words)):
        lines=[' '.join(words[:split]),' '.join(words[split:])]
        widths=[width(line) for line in lines]
        if max(widths)<=max_width:candidates.append((abs(widths[0]-widths[1]),lines))
    if not candidates:raise ValueError('Nom trop long pour les deux lignes du gabarit fixe ; revoir la composition sans réduire la typographie.')
    return min(candidates,key=lambda x:x[0])[1]


def render_cover(photo,name,location,directory):
    style=COVER_STYLE
    # Shared sizes and exact font files: never resize a title to fill its tile.
    face=ImageFont.truetype(style['title_font'],style['title_size'])
    badgefont=ImageFont.truetype(style['badge_font'],style['badge_size'])
    text=' '.join(name.upper().split());label=' '.join(location.upper().split())
    if not text or not label:raise ValueError('Nom et localisation requis.')
    lines=title_lines(text,face,style['title_max_width'])
    box=badgefont.getbbox(label,anchor='ls');badge=style['badge_bounds']
    if box[2]-box[0]>badge[2]-badge[0]-2*style['badge_horizontal_padding']:
        raise ValueError('Localisation trop longue pour le cartouche fixe.')
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    with Image.open(photo) as original:
        canvas=ImageOps.fit(ImageOps.exif_transpose(original).convert('RGB'),tuple(style['canvas']),method=Image.Resampling.LANCZOS)
    canvas=Image.blend(canvas,Image.new('RGB',canvas.size,'black'),style['photo_darkening'])
    draw=ImageDraw.Draw(canvas);cap_box=face.getbbox('H',anchor='ls');cap_height=cap_box[3]-cap_box[1]
    line_height=cap_height+style['title_line_gap'];baselines=[]
    for index,line in enumerate(lines):
        linebox=face.getbbox(line,anchor='ls')
        x=style['title_center'][0]-(linebox[2]-linebox[0])/2-linebox[0]
        baseline=round(style['title_center'][1]+cap_height/2+(index-(len(lines)-1)/2)*line_height)
        draw.text((x,baseline),line,font=face,fill=style['text_color'],anchor='ls',stroke_width=style['stroke_width']);baselines.append(baseline)
    draw.rectangle(tuple(badge),fill=style['badge_color'])
    # One baseline for every location, independent of its letters or length.
    badge_cap=badgefont.getbbox('H',anchor='ls');badge_baseline=round((badge[1]+badge[3])/2-(badge_cap[1]+badge_cap[3])/2)
    draw.text(((badge[0]+badge[2])/2-(box[2]-box[0])/2-box[0],badge_baseline),label,font=badgefont,fill=style['text_color'],anchor='ls')
    canvas.save(directory/'cover.png');canvas.crop(tuple(style['grid_crop'])).save(directory/'grid.png')
    return {'dimensions':style['canvas'],'grid_dimensions':[1080,1440],'template':'B-enseigne-v1','style_version':style['version'],'title_lines':lines,
            'style':dict(style),'title_cap_height':cap_height,'title_baselines':baselines,'badge_baseline':badge_baseline,
            'font_sha256':{'title':sha(style['title_font']),'badge':sha(style['badge_font'])}}

class ReelCovers:
    def __init__(self,workflow,pipeline):
        self.workflow=workflow;self.pipeline=pipeline;self.store=AcquisitionStore(pipeline.store.root/'covers')
    def settings(self):
        path=self.pipeline.repository/'brands/example/cover.json'
        return json.loads(path.read_text()) if path.exists() else {}
    def list(self):
        return {v['run_id']:v for p in self.store.root.glob('*.json') if (v:=json.loads(p.read_text())).get('run_id')}
    def prepare(self,review):
        if review['kind']!='run' or review['decision']!='keep':return None
        from app.services.editorial_inbox import EditorialInbox
        rows=EditorialInbox(self.workflow,self.pipeline).list()
        if review not in rows:raise ValueError('Retour propriétaire authentique requis.')
        current=[r for r in rows if r['kind']=='run' and r['target_id']==review['target_id']][-1]
        if current['id']!=review['id']:return None
        config=self.settings();place=review['place_id'];audit=self.workflow.store.read('visual-audits.json',{}).get(place,{})
        identity=hashlib.sha256(json.dumps([config,audit,review['id'],COVER_STYLE],sort_keys=True).encode()).hexdigest()
        previous=self.store.read(review['id']+'.json')
        if previous and previous.get('identity')==identity:return previous
        result=dict(run_id=review['target_id'],review_id=review['id'],place_id=place,identity=identity,template='B-enseigne-v1',created_at=datetime.now(UTC).isoformat(),publication_approval=False)
        try:
            if config.get('selected_template')!='B-enseigne-v1':raise ValueError('Template de couverture non sélectionné.')
            loc=config.get('locations',{}).get(place)
            if not loc or not loc.get('source'):raise ValueError('Localisation sourcée à renseigner avant génération.')
            run=EditorialInbox(self.workflow,self.pipeline).target('run',review['target_id'])
            if run['place_id']!=place:raise ValueError('Lieu incohérent.')
            selected=config.get('photos',{}).get(place)
            if selected:
                photo=self.pipeline.store.path(selected['path']);source=selected['source']
                if sha(photo)!=selected['sha256']:raise ValueError('Photo source modifiée.')
            else:
                candidates=audit.get('selection',[])
                selected=next((c for c in candidates if re.search('façade|facade|devanture|entrée',c.get('caption',''),re.I)),candidates[0] if candidates else None)
                if not selected:raise ValueError('Photo sourcée à sélectionner.')
                photo=self.pipeline.store.path(selected['asset_path']);source=selected.get('source')
                if not source or sha(photo)!=selected.get('sha256'):raise ValueError('Photo ou provenance incohérente.')
            output=self.store.path(review['id']+'/'+identity[:12]);name=self.workflow.read()['places'][place]['name']
            result.update(render_cover(photo,name,loc['label'],output),status='ready',name=name,location=loc['label'],location_source=loc['source'],photo_source=source,photo_sha256=sha(photo))
            for key,file in [('image','cover.png'),('grid','grid.png')]:result[key]={'path':str((output/file).relative_to(self.pipeline.store.root)),'sha256':sha(output/file)}
        except Exception as e:result.update(status='blocked',message=str(e))
        if previous and previous.get('identity')!=identity:
            history='versions/'+review['id']+'/'+previous['identity']+'.json'
            if self.store.read(history,previous)!=previous:
                revision=hashlib.sha256(json.dumps(previous,sort_keys=True).encode()).hexdigest()[:16]
                history='versions/'+review['id']+'/'+previous['identity']+'-'+revision+'.json'
            self.store.write(history,previous,immutable=True)
            result.update(previous_identity=previous['identity'],previous_record=history)
        self.store.write(review['id']+'.json',result);return result
