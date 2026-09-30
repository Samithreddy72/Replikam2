import {test,expect} from '@playwright/test';
test('compact workspace keeps all basic controls above the action bar',async({page})=>{
 for(const size of [{width:1360,height:900},{width:960,height:680}]){
  await page.setViewportSize(size);await page.goto('/?preview=studio');
  await expect(page.getByLabel('Camera',{exact:true})).toHaveValue('Built-in camera');
  const bottom=(await page.locator('.session-action-area').boundingBox())!.y;
  for(const control of [page.getByLabel('Meeting bridge',{exact:true}),page.getByLabel('Bridge PIN',{exact:true}),page.getByLabel('Camera',{exact:true}),page.getByLabel('Microphone',{exact:true}),page.getByRole('button',{name:'Test speakers',exact:true}),page.getByRole('button',{name:'Enable local preview',exact:true})]){
   const b=await control.boundingBox();expect(b).not.toBeNull();expect(b!.y).toBeGreaterThanOrEqual(0);expect(b!.y+b!.height).toBeLessThan(bottom);
  }
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBeTruthy();
  await page.screenshot({path:`test-results/studio-ux-${size.width}.png`,fullPage:false});
 }
});
test('missing and wrong PIN are errors with an inline correction',async({page})=>{
 await page.goto('/?preview=studio');await page.getByRole('button',{name:'Go live',exact:true}).click();
 await expect(page.getByRole('alert')).toContainText('Bridge PIN is required');
 await expect(page.getByLabel('Bridge PIN',{exact:true})).toBeFocused();
 await expect(page.getByLabel('Bridge PIN',{exact:true})).toHaveAttribute('aria-invalid','true');
 await page.getByLabel('Bridge PIN',{exact:true}).fill('000000');await page.getByRole('button',{name:'Go live',exact:true}).click();
 await expect(page.getByRole('alert')).toContainText('Incorrect bridge PIN');await expect(page.getByRole('alert')).toHaveClass(/error/);
 await page.screenshot({path:'test-results/pin-error.png'});
 await page.getByLabel('Bridge PIN',{exact:true}).fill('123456');await page.getByRole('button',{name:'Go live',exact:true}).click();
 await expect(page.getByRole('button',{name:'End session',exact:true})).toBeVisible();
});
test('camera and mic can start off, toggle separately, and stop',async({page})=>{
 await page.goto('/?preview=studio');
 await page.getByRole('button',{name:'Turn camera off',exact:true}).click();await page.getByRole('button',{name:'Mute microphone',exact:true}).click();
 await page.getByLabel('Bridge PIN',{exact:true}).fill('123456');await page.getByRole('button',{name:'Go live',exact:true}).click();
 await expect(page.getByRole('button',{name:'Turn camera on',exact:true})).toBeVisible();await expect(page.getByRole('button',{name:'Unmute microphone',exact:true})).toBeVisible();
 await page.getByRole('button',{name:'Turn camera on',exact:true}).click();await expect(page.getByRole('button',{name:'Unmute microphone',exact:true})).toBeVisible();
 await page.getByRole('button',{name:'Unmute microphone',exact:true}).click();await page.getByRole('button',{name:'End session',exact:true}).click();
 await expect(page.getByRole('button',{name:'Go live',exact:true})).toBeEnabled();
});
test('setup and help use dismissible focused dialogs',async({page})=>{
 await page.goto('/?preview=studio');await page.getByRole('button',{name:'Check my setup',exact:true}).click();
 const setup=page.getByRole('dialog',{name:'Setup check',exact:true});await expect(setup).toBeVisible();await expect(setup).toBeFocused();
 await page.screenshot({path:'test-results/setup-dialog.png'});await page.keyboard.press('Escape');await expect(setup).toHaveCount(0);await expect(page.getByRole('button',{name:'Check my setup',exact:true})).toBeFocused();
 await page.getByRole('button',{name:'Get help',exact:true}).click();await expect(page.getByRole('dialog',{name:'Get help',exact:true})).toBeVisible();
 await page.screenshot({path:'test-results/help-dialog.png'});await page.getByRole('button',{name:'Close Get help',exact:true}).click();
});
test('history migrates old entries and loads eight per page',async({page})=>{
 await page.addInitScript(()=>localStorage.setItem('nb.sessions',JSON.stringify(Array.from({length:19},(_,i)=>({bridge:'Room '+i,started:new Date(Date.UTC(2026,8,30,10,i)).toISOString(),ended:new Date(Date.UTC(2026,8,30,10,i+1)).toISOString()})))));
 await page.goto('/?preview=studio');await page.getByRole('button',{name:'Sessions',exact:true}).click();
 await expect(page.locator('.session-list > div')).toHaveCount(8);await expect(page.getByText('Room 18',{exact:true})).toBeVisible();await expect(page.getByRole('button',{name:'Previous',exact:true})).toBeDisabled();
 await page.getByRole('button',{name:'Next',exact:true}).click();await expect(page.getByText('Room 10',{exact:true})).toBeVisible();await expect(page.locator('.session-list > div')).toHaveCount(8);
 await page.getByRole('button',{name:'Next',exact:true}).click();await expect(page.locator('.session-list > div')).toHaveCount(3);await expect(page.getByRole('button',{name:'Next',exact:true})).toBeDisabled();
 await page.getByRole('button',{name:'Previous',exact:true}).click();await expect(page.getByText('Room 10',{exact:true})).toBeVisible();await page.screenshot({path:'test-results/sessions-paged.png'});
});
