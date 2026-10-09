// Run on your own logged-in Spoon profile in the browser console.
// Uses Spoon's existing API client. Exports IDs/names only, never login credentials.
(async () => {
  'use strict';
  if (location.origin !== 'https://www.spooncast.net') throw new Error('Spoonにログインしたブラウザで実行してください。');
  const match = location.pathname.match(/^\/jp\/channel\/(\d+)\//);
  if (!match) throw new Error('ご自身のプロフィールページを開いてください。');
  const ownerId = match[1];
  // Verified official bundle exports. Fail closed if Spoon replaces this build.
  const module = await import('/assets/C8nhyTLt.js');
  const client = module.q, base = module.x, routes = module.U;
  if (!client || typeof client.get !== 'function' || base !== 'https://jp-api.spooncast.net/' || typeof routes?.followers !== 'function') {
    throw new Error('Spoonの画面仕様が変わりました。この取得スクリプトの更新が必要です。');
  }
  const me = (await client.get(base + 'users/me/')).data;
  if (!Array.isArray(me.results) || me.results.length !== 1 || String(me.results[0].id) !== ownerId) {
    throw new Error('ログイン中のご自身のプロフィールを開いてください。');
  }
  const canonical = raw => {
    if (!raw || !Number.isSafeInteger(raw.id) || raw.id <= 0 || typeof raw.nickname !== 'string') throw new Error('ファン一覧の形式が変わりました。');
    if (raw.tag != null && typeof raw.tag !== 'string') throw new Error('プロフィールIDの形式が変わりました。');
    return {id: String(raw.id), name: raw.nickname, tag: raw.tag || null};
  };
  const owner = canonical(me.results[0]), followers = new Map(), seen = new Set();
  const endpoint = new URL(routes.followers(Number(ownerId)), base);
  let url = endpoint.href;
  try {
    while (url) {
      if (seen.has(url)) throw new Error('ページの循環を検出しました。');
      seen.add(url);
      const data = (await client.get(url)).data;
      if (data.status_code != null && data.status_code !== 200 || !Array.isArray(data.results)) throw new Error('ファン一覧を取得できませんでした。');
      for (const raw of data.results) {const user = canonical(raw); followers.set(user.id, user);}
      console.info(`SpoonDev: ${followers.size} 人のファンを取得`);
      if (!data.next) break;
      if (typeof data.next !== 'string') throw new Error('次ページの形式が変わりました。');
      let cursor;
      if (data.next.startsWith('https://')) {
        const next = new URL(data.next);
        if (next.origin !== endpoint.origin || next.pathname !== endpoint.pathname || next.hash) throw new Error('次ページの取得先が変わりました。');
        cursor = next.searchParams.get('cursor');
      } else {
        if (/^[/?#]|:/.test(data.next)) throw new Error('次ページの形式が変わりました。');
        cursor = data.next;
      }
      if (!cursor) throw new Error('次ページのカーソルがありません。');
      const next = new URL(endpoint); next.searchParams.set('cursor', cursor); url = next.href;
      await new Promise(resolve => setTimeout(resolve, 400));
    }
  } catch (error) {
    throw new Error(`取得を中止しました。完全な一覧は保存していません。時間をおいて再実行してください。取得済み ${followers.size} 人。${error.message}`);
  }
  const payload = {owner, followers: [...followers.values()], complete: true, exported_at: new Date().toISOString()};
  const blob = new Blob([JSON.stringify(payload, null, 2)], {type: 'application/json'});
  const link = document.createElement('a'), download = URL.createObjectURL(blob);
  link.href = download; link.download = `spoon-fans-${ownerId}.json`; document.body.append(link); link.click(); link.remove();
  setTimeout(() => URL.revokeObjectURL(download), 60000);
  console.info(`SpoonDev: 完了、${followers.size} 人。保存したJSONをSpoonDevに取り込んでください。`);
})();
