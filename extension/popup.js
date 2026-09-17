document.querySelector('#run').onclick = async () => {
  const result = document.querySelector('#result');
  const [tab] = await chrome.tabs.query({active: true, currentWindow: true});
  try {
    const response = await fetch('http://127.0.0.1:8099/runs', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({url: tab.url, allow_mutations: false})
    });
    result.textContent = JSON.stringify(await response.json(), null, 2);
  } catch (error) { result.textContent = 'Start Satya service on port 8099 first.\n' + error; }
};
