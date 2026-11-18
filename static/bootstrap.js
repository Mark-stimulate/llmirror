/* Keep the startup diagnostic compatible with older Chromium browsers. */
(function () {
  var box = document.getElementById('auth-box');
  var ready = false;
  var timer;
  function fail(message) {
    if (ready) return;
    clearTimeout(timer);
    box.innerHTML = '<div class="eyebrow">STARTUP DIAGNOSTIC</div><h2>工作台未能完成加载</h2><p id="startup-message"></p><button class="button primary" id="startup-retry">重新加载</button><p class="auth-footnote">请确认启动窗口仍在运行。若重试无效，将本提示反馈给维护者。</p>';
    document.getElementById('startup-message').textContent = message;
    document.getElementById('startup-retry').onclick = function () { location.reload(); };
  }
  window.addEventListener('llmirror-ready', function () { ready = true; clearTimeout(timer); });
  window.addEventListener('error', function (event) {
    if (!ready) fail('页面脚本异常：' + (event.message || '脚本资源加载失败。请按 Ctrl+F5 强制刷新。'));
  });
  window.addEventListener('unhandledrejection', function (event) {
    if (!ready) fail('启动失败：' + (event.reason && event.reason.message ? event.reason.message : '请刷新后重试。'));
  });
  var script = document.createElement('script');
  script.src = '/app.js?v=1.2.3';
  script.charset = 'utf-8';
  script.onerror = function () { fail('主程序脚本加载失败。请按 Ctrl+F5 强制刷新，或使用新版 Edge/Chrome 浏览器。'); };
  timer = setTimeout(function () { fail('连接超过15秒。请确认启动窗口仍在运行，再点击重新加载。'); }, 15000);
  document.head.appendChild(script);
})();
