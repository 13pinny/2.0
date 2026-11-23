const el = document.getElementById('base');
chrome.storage.sync.get({ base: 'https://kartis.homes' }, (v) => { el.value = v.base; });
document.getElementById('save').onclick = () => {
  chrome.storage.sync.set({ base: el.value.trim() || 'https://kartis.homes' }, () => {
    document.getElementById('ok').textContent = 'Saved';
  });
};
