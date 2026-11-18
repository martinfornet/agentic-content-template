// Browser integration test against a local studio. Requires playwright-core and Chromium.
const { chromium } = require('playwright-core');
const fs = require('node:fs/promises');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const assert = require('node:assert/strict');

(async () => {
  const target = process.env.STUDIO_URL || 'http://127.0.0.1:8765';
  assert(['localhost','127.0.0.1'].includes(new URL(target).hostname));
  await fs.mkdir('output', {recursive:true});
  const dir = path.resolve(await fs.mkdtemp('output/browser-demo-'));
  const clips = ['orange','blue'].map(color => {
    const name = path.join(dir, color+'.mp4');
    execFileSync('ffmpeg',['-v','error','-f','lavfi','-i','color=c='+color+':s=360x640:r=30',
      '-t','6','-c:v','libx264',name]);
    return name;
  });
  const browser = await chromium.launch({executablePath:process.env.CHROMIUM || undefined});
  try {
    const page = await browser.newPage({viewport:{width:1360,height:900}});
    page.setDefaultTimeout(300000);
    const errors=[];
    page.on('pageerror',err=>errors.push(err.message));
    async function click(label) {
      const response = page.waitForResponse(r=>r.request().method()==='POST' && r.url().startsWith(target+'/api/'));
      await page.getByRole('button',{name:label,exact:true}).click();
      const r=await response;
      if(!r.ok()) throw Error(label+': '+await r.text());
      console.log(label);
      return await r.json();
    }
    await page.goto(target);
    await page.getByLabel('Direction créative').fill('Deux plans de couleur, orange puis bleu. Propose une courte narration française très simple.');
    const project = await click('Générer le brief');
    await page.getByLabel('Ajouter des vidéos').setInputFiles(clips);
    await page.getByLabel('Nom pour les validations').fill('test-technique');
    assert.equal(await page.locator('#takes video').count(),2);
    await click('J’ai visionné et je valide ces prises');
    await page.getByRole('button',{name:'Générer la voix Chatterbox'}).waitFor();
    await page.reload();
    await click('Générer la voix Chatterbox');
    await click('Préparer les deux mix');
    await page.getByLabel('Bande-son à monter').selectOption('A');
    const audio = page.locator('audio').nth(1);
    await audio.evaluate(async el=>{el.muted=true; await el.play(); el.pause();});
    await click('J’ai écouté et je valide la bande-son');
    await click('Lancer le montage');
    const video=page.locator('main video');
    await video.evaluate(async el=>{el.muted=true; await el.play(); el.pause();});
    await page.screenshot({path:path.join(dir,'studio.png'),fullPage:true});
    await click('J’ai regardé et je valide le MP4');
    const download=page.waitForEvent('download');
    await page.getByRole('link',{name:'Télécharger le MP4'}).click();
    const output=path.join(dir,'montage.mp4');
    await (await download).saveAs(output);
    execFileSync('ffmpeg',['-v','error','-xerror','-i',output,'-f','null','-']);
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({project:project.id,output}));
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
