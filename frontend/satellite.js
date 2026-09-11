// 卫星跟踪前端逻辑
(function () {
  const $ = id => document.getElementById(id);
  let map = null;
  let currentNorad = null;
  let currentSatName = null; // 当前选中卫星名称
  let trackLayers = {};   // norad -> L.polyline
  let satPosMarkers = {}; // norad -> {circle, label}
  let currentMarker = null;
  let coverageLayer = null; // 卫星覆盖范围圆
  let followSat = false;    // 是否跟随卫星 (点击卫星后开启, 手动拖动地图时关闭)
  let tracking = false;
  let radarPoints = [];
  let radarPass = null;   // 当前显示过境的 pass 元数据 (AOS 标记)
  let radarTimer = null;
  let infoTimer = null;
  let obsMarker = null;

  // ===== 地图初始化 (天地图矢量瓦片, Web Mercator 投影) =====
  // ⚠️ 需要使用天地图开发者 token. 免费申请: https://uums.tianditu.gov.cn/register
  // 注册后 → 控制台 → 创建新应用 (选择"浏览器端") → 获取 Key, 填入下方 TDT_KEY
  const TDT_KEY = 'd0ca322ca9f024d7673cd4d91e588290';  // ← 天地图 API Key

  function initMap() {
    map = L.map('map', {
      minZoom: 2, maxZoom: 18,
      zoomControl: false,
      worldCopyJump: true   // 拖过边缘自动跳转
    });
    // 缩放按钮放置右下角
    L.control.zoom({ position: 'bottomright' }).addTo(map);
    window.map = map;
// 底图: 天地图矢量瓦片 (含行政边界) — 通过后端代理绕过 WAF
    L.tileLayer('/tdt/vec/{z}/{x}/{y}', {
      maxZoom: 18,
      attribution: '&copy; 天地图'
    }).addTo(map);
    // 天地图行政标注层 (地名、行政名称)
    if (TDT_KEY) {
      L.tileLayer('/tdt/cva/{z}/{x}/{y}', {
        maxZoom: 18,
        attribution: '&copy; 天地图'
      }).addTo(map);
    }
    // OSM 兜底: 如果天地图 vec_w 因网络问题未加载, 不额外处理
    // 以观测站为中心并显示观测站位置
    fetch('/api/sat/observer').then(r => r.json()).then(d => {
      if (d.ok) {
        map.setView([d.lat, d.lon], 3);
        window.obsPos = [d.lat, d.lon];
        obsMarker = L.circleMarker([d.lat, d.lon], {
          radius: 6, color: '#ef4444', fillColor: '#ef4444', fillOpacity: 1
        }).addTo(map).bindPopup('观测站');
      }
    }).catch(() => { map.setView([20, 0], 2); window.obsPos = [20, 0]; });
    // ponytail: 暴露观测站标记给 inline script 画方向线
    window.obsMarker = obsMarker;
  }

  // 保存观测站后刷新地图中心与标记
  async function updateObserverOnMap() {
    try {
      const r = await fetch('/api/sat/observer');
      const d = await r.json();
      if (d.ok) {
        map.setView([d.lat, d.lon], 3);
        window.obsPos = [d.lat, d.lon];
        if (obsMarker) map.removeLayer(obsMarker);
        obsMarker = L.circleMarker([d.lat, d.lon], {
          radius: 6, color: '#ef4444', fillColor: '#ef4444', fillOpacity: 1
        }).addTo(map).bindPopup('观测站');
        window.obsMarker = obsMarker;
      }
    } catch (e) { /* 忽略 */ }
  }
  window.updateObserverOnMap = updateObserverOnMap;

  // ===== 倒计时格式化 =====
  function formatCountdown(sec) {
    sec = Math.floor(sec);
    if (sec <= 0) return '正在过境';
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = sec % 60;
    if (h > 0) return `${h}小时${m}分`;
    if (m > 0) return `${m}分${s}秒`;
    return `${s}秒`;
  }

  // ===== 收藏列表 (按下次过境时间排序) =====
  async function loadFavorites() {
    try {
      const res = await fetch('/api/sat/favorites');
      const data = await res.json();
      if (!data.ok) return;
      const list = $('favList');
      list.innerHTML = '';
      const now = Date.now() / 1000;
      data.favorites.forEach(f => {
        const item = document.createElement('div');
        item.className = 'fav-item' + (f.norad === currentNorad ? ' active' : '');
        item.dataset.aos = f.next_aos || '';
        item.dataset.status = f.status || '';
        let infoHtml = '';
        if (f.status === 'in_pass') {
          infoHtml = `<span class="countdown in-pass">正在过境</span> · 最大仰角 ${f.max_el}°`;
        } else if (f.status === 'upcoming' && f.next_aos) {
          const sec = Math.max(0, f.next_aos - now);
          const urgentClass = sec < 1800 ? ' urgent' : '';
          infoHtml = `<span class="countdown${urgentClass}">${formatCountdown(sec)}后过境</span> · 最大仰角 ${f.max_el}°`;
        } else {
          infoHtml = `<span class="countdown">未来48小时无过境</span>`;
        }
        item.innerHTML = `<div><div class="name">${f.name || f.norad}</div><div class="norad">NORAD ${f.norad}</div><div class="pass-info">${infoHtml}</div></div>
          <button class="del" data-norad="${f.norad}">删除</button>`;
        item.addEventListener('click', (e) => {
          if (e.target.classList.contains('del')) return;
          selectSatellite(f.norad, f.name || f.norad);
        });
        item.querySelector('.del').addEventListener('click', async (e) => {
          e.stopPropagation();
          const res = await fetch('/api/sat/favorites/' + f.norad, { method: 'DELETE' });
          const d = await res.json().catch(() => ({}));
          if (!d.ok) { toast('删除失败: ' + (d.error || d.detail || '')); return; }
          delete trackLayers[f.norad];
          if (satPosMarkers[f.norad]) {
            map.removeLayer(satPosMarkers[f.norad].circle);
            map.removeLayer(satPosMarkers[f.norad].label);
            delete satPosMarkers[f.norad];
          }
          if (f.norad === currentNorad) { currentNorad = null; clearSatellite(); }
          loadFavorites();
        });
        list.appendChild(item);
      });
      // 未选中卫星: 显示实时位置 (圆圈+名称); 选中卫星: 显示轨迹
      data.favorites.forEach(f => {
        if (f.norad === currentNorad) loadTrack(f.norad, trackHours);
        else loadSatPos(f.norad, f.name || f.norad);
      });
      restoreLastSelection(data.favorites);
    } catch (e) { /* 忽略 */ }
  }

  // ===== 恢复上次选中卫星 (优先: 跟踪中的 → localStorage 的) =====
  let restoredOnce = false;  // 只自动恢复一次, 之后尊重用户手动取消选中的状态
  function restoreLastSelection(favorites) {
    if (currentNorad || restoredOnce) return;
    // 1) 刷新时后端仍在跟踪的卫星
    if (pendingRestoreNorad != null) {
      const hit = favorites.find(f => String(f.norad) === String(pendingRestoreNorad));
      if (hit) {
        restoredOnce = true;
        const n = pendingRestoreNorad;
        pendingRestoreNorad = null;
        selectSatellite(Number(n), hit.name || n);
        return;
      }
    }
    pendingRestoreNorad = null;
    // 2) 上次页面选中的卫星
    try {
      const last = JSON.parse(localStorage.getItem('lastSat') || 'null');
      if (last && last.norad != null) {
        const hit = favorites.find(f => String(f.norad) === String(last.norad));
        if (hit) {
          restoredOnce = true;
          selectSatellite(hit.norad, hit.name || last.norad);
        }
      }
    } catch (e) { /* 忽略 */ }
  }

  // 每秒更新收藏列表倒计时
  setInterval(() => {
    const now = Date.now() / 1000;
    document.querySelectorAll('.fav-item').forEach(item => {
      const el = item.querySelector('.countdown');
      if (!el) return;
      const status = item.dataset.status;
      const aos = parseFloat(item.dataset.aos);
      if (status === 'in_pass') {
        el.textContent = '正在过境';
        el.className = 'countdown in-pass';
      } else if (status === 'upcoming' && aos) {
        const sec = Math.max(0, aos - now);
        el.textContent = formatCountdown(sec) + '后过境';
        el.className = 'countdown' + (sec < 1800 ? ' urgent' : '');
      }
    });
  }, 1000);

  // ===== 未选中卫星实时位置 (圆圈 + 名称) =====
  async function loadSatPos(norad, name) {
    try {
      const res = await fetch('/api/sat/position/' + norad);
      const data = await res.json();
      if (!data.ok) return;
      if (satPosMarkers[norad]) {
        map.removeLayer(satPosMarkers[norad].circle);
        map.removeLayer(satPosMarkers[norad].label);
      }
      const circle = L.circleMarker([data.sub_lat, data.sub_lon], {
        radius: 5, color: '#facc15', fillColor: '#facc15', fillOpacity: 0.8,
        className: 'sat-dot-clickable'
      }).addTo(map);
      const label = L.marker([data.sub_lat, data.sub_lon], {
        icon: L.divIcon({ className: 'sat-label clickable', html: name, iconSize: [90, 16] })
      }).addTo(map);
      // 点击地图上的卫星圆点/名称 → 直接切换选中
      const pick = () => selectSatellite(norad, name);
      circle.on('click', pick);
      label.on('click', pick);
      satPosMarkers[norad] = { circle, label };
    } catch (e) { /* 忽略 */ }
  }

  // ===== 卫星实时搜索下拉 (名称/编号模糊匹配, 点击即收藏) =====
  const favSearchRes = $('favSearchRes');
  let searchTimer = null;

  function hideSearchRes() { favSearchRes.style.display = 'none'; }

  async function doLiveSearch(q) {
    try {
      const res = await fetch('/api/sat/search?q=' + encodeURIComponent(q));
      const data = await res.json();
      if (!data.ok) { hideSearchRes(); return; }
      const matches = data.satellites || [];
      if (matches.length === 0) {
        favSearchRes.innerHTML = '<div class="res-empty">未找到匹配卫星</div>';
        favSearchRes.style.display = 'block';
        return;
      }
      favSearchRes.innerHTML = matches.map(s =>
        `<div class="res-item" data-norad="${s.norad}"><span class="r-name">${s.name || s.norad}</span><span class="r-norad">NORAD ${s.norad}</span></div>`
      ).join('');
      favSearchRes.style.display = 'block';
      favSearchRes.querySelectorAll('.res-item').forEach(el => {
        el.addEventListener('click', () => {
          addFav(el.dataset.norad);
          $('favSearch').value = '';
          hideSearchRes();
        });
      });
    } catch (e) { hideSearchRes(); }
  }

  $('favSearch').addEventListener('input', () => {
    const q = $('favSearch').value.trim();
    clearTimeout(searchTimer);
    if (!q) { hideSearchRes(); return; }
    searchTimer = setTimeout(() => doLiveSearch(q), 250);
  });
  document.addEventListener('click', (e) => {
    if (!e.target.closest('.fav-search-wrap')) hideSearchRes();
  });

  // ===== 添加收藏 =====
  $('btnFavAdd').onclick = async () => {
    const q = $('favSearch').value.trim();
    if (!q) { toast('请输入搜索词'); return; }
    try {
      // 后端检索: 名称/编号包含匹配, 最多返回 20 条, 无需拉全量 TLE
      const res = await fetch('/api/sat/search?q=' + encodeURIComponent(q));
      const data = await res.json();
      if (!data.ok) return;
      const matches = data.satellites;
      if (matches.length === 0) { toast('未找到卫星'); return; }
      if (matches.length === 1) {
        await addFav(matches[0].norad);
      } else {
        // 多个匹配, 用第一个
        await addFav(matches[0].norad);
        toast(`找到 ${matches.length} 个, 已添加第一个`);
      }
    } catch (e) { toast('搜索失败'); }
  };
  async function addFav(norad) {
    try {
      const res = await fetch('/api/sat/favorites', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ norad }) });
      const data = await res.json();
      if (data.ok) { toast('已添加收藏'); loadFavorites(); }
      else toast('添加失败: ' + (data.detail || ''));
    } catch (e) { toast('添加失败'); }
  }

  // ===== 经度展开 (跨日界线连续化, 避免横穿地图的长线) =====
  function unwrapLng(points) {
    if (points.length === 0) return [];
    const result = [[points[0][0], points[0][1]]];
    let offset = 0;
    for (let i = 1; i < points.length; i++) {
      let dlon = points[i][1] - points[i - 1][1];
      if (dlon > 180) offset -= 360;       // 179 -> -179, 补偿 -360
      else if (dlon < -180) offset += 360;  // -179 -> 179, 补偿 +360
      result.push([points[i][0], points[i][1] + offset]);
    }
    return result;
  }

  // ===== 加载卫星轨迹 (星下点) =====
  async function loadTrack(norad, hours, seq) {
    try {
      const res = await fetch('/api/sat/track/' + norad + '?hours=' + (hours || 24) + '&step=120');
      const data = await res.json();
      if (seq !== undefined && seq !== selectSeq) return;  // 已切换到其他卫星, 丢弃旧响应
      if (!data.ok || !data.points || data.points.length === 0) return;
      const raw = data.points.map(p => [p.lat, p.lon]);
      if (trackLayers[norad]) {
        map.removeLayer(trackLayers[norad]);
        trackLayers[norad] = null;
      }
      // 展开经度使轨迹连续, 然后渲染 3 份 (-360/0/+360) 保证左右滚动都有轨迹
      const unwrapped = unwrapLng(raw);
      const layers = [];
      for (let off = -360; off <= 360; off += 360) {
        const pts = unwrapped.map(p => [p[0], p[1] + off]);
        layers.push(L.polyline(pts, { color: '#0ea5e9', weight: 2, opacity: 0.8 }).addTo(map));
      }
      trackLayers[norad] = L.layerGroup(layers).addTo(map);
    } catch (e) { /* 忽略 */ }
  }

  // ===== 轨迹时长切换 (1h/3h/6h/12h) =====
  let trackHours = 12;  // 地图轨迹默认时长
  function bindTrackDurBtns() {
    document.querySelectorAll('#durBtns .dur-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        trackHours = parseInt(btn.dataset.hours, 10);
        document.querySelectorAll('#durBtns .dur-btn').forEach(b => b.classList.toggle('active', b === btn));
        if (currentNorad) loadTrack(currentNorad, trackHours);
      });
    });
  }

  // ===== 选择卫星 =====
  function clearAllTracks() {
    Object.keys(trackLayers).forEach(k => { if (trackLayers[k]) map.removeLayer(trackLayers[k]); });
    trackLayers = {};
  }
  let selectSeq = 0;  // 切换令牌: 快速连续切换时旧请求响应直接丢弃, 防止旧数据覆盖新状态
  async function selectSatellite(norad, name) {
    const seq = ++selectSeq;
    // 切换卫星时移除旧轨迹
    clearAllTracks();
    currentNorad = norad;
    currentSatName = name || norad;
    try { localStorage.setItem('lastSat', JSON.stringify({ norad, name: currentSatName })); } catch (e) { /* 忽略 */ }
    // 高亮列表
    document.querySelectorAll('.fav-item').forEach(el => el.classList.remove('active'));
    const items = document.querySelectorAll('.fav-item');
    items.forEach(el => {
      // 太阳/月球等天体项无 .del 按钮, 需判空
      const delBtn = el.querySelector('.del');
      if (delBtn && String(delBtn.dataset.norad) === String(norad)) el.classList.add('active');
    });
    // 移除该卫星的实时位置圆圈, 显示 24 小时轨迹
    if (satPosMarkers[norad]) {
      map.removeLayer(satPosMarkers[norad].circle);
      map.removeLayer(satPosMarkers[norad].label);
      delete satPosMarkers[norad];
    }
    loadTrack(norad, trackHours, seq);
    loadRadar(norad, seq);
    loadPasses(norad, seq);
    startInfoPoll(norad, seq);
    // 点击卫星: 开启跟随, 地图中心移至卫星
    followSat = true;
    map.once('dragstart', () => { followSat = false; });  // 手动拖动时取消跟随
  }

  function clearSatellite() {
    currentNorad = null;
    currentSatName = null;
    followSat = false;
    try { localStorage.removeItem('lastSat'); } catch (e) { /* 忽略 */ }
    $('satInfo').innerHTML = '<div class="row"><span class="k">未选择卫星</span></div>';
    if (currentMarker) { map.removeLayer(currentMarker); currentMarker = null; }
    if (coverageLayer) { map.removeLayer(coverageLayer); coverageLayer = null; }
    if (radarTimer) clearInterval(radarTimer);
    if (infoTimer) clearInterval(infoTimer);
    $('passList').innerHTML = '';
    drawRadar([]);
    radarPass = null;
    // 移除所有轨迹, 恢复实时位置圆圈
    Object.keys(trackLayers).forEach(k => { if (trackLayers[k]) map.removeLayer(trackLayers[k]); });
    trackLayers = {};
    loadFavorites();
  }

  // 卫星覆盖圆半径: 仰角>=0 的地面范围 (米)
  function coverageRadius(altKm) {
    const R = 6371;
    const h = Math.max(altKm, 100);
    const gamma = Math.acos(R / (R + h)); // 地心角 rad
    return gamma * R * 1000;
  }
  // 绘制/更新覆盖圆 (圆心跟随星下点)
  function drawCoverage(subLat, subLon, altKm) {
    const latlng = [subLat, subLon];
    if (coverageLayer) { coverageLayer.setLatLng(latlng); return; }
    coverageLayer = L.circle(latlng, {
      radius: coverageRadius(altKm),
      color: '#0ea5e9', weight: 1,
      fillColor: '#0ea5e9', fillOpacity: 0.08
    }).addTo(map);
  }

  // ===== 卫星信息轮询 =====
  function startInfoPoll(norad, seq) {
    if (infoTimer) clearInterval(infoTimer);
    const update = async () => {
      try {
        const res = await fetch('/api/sat/position/' + norad);
        const data = await res.json();
        if (seq !== undefined && seq !== selectSeq) return;  // 已切换, 丢弃旧响应
        if (!data.ok) return;
        $('satInfo').innerHTML = `
          <div class="row"><span class="k">方位角</span><span class="v">${data.azimuth}°</span></div>
          <div class="row"><span class="k">仰角</span><span class="v">${data.elevation}°</span></div>
          <div class="row"><span class="k">距离</span><span class="v">${data.distance} km</span></div>
          <div class="row"><span class="k">可见</span><span class="v" style="color:${data.visible ? '#4ade80' : '#ef4444'}">${data.visible ? '是' : '否'}</span></div>`;
        // 覆盖范围圆 (跟随星下点)
        drawCoverage(data.sub_lat, data.sub_lon, data.alt_km);
        // 地图上当前星下点 (脉冲高亮标记 + 卫星名称标签, 与未选中的黄色圆点区分)
        if (currentMarker) map.removeLayer(currentMarker);
        const labelText = currentSatName || '卫星';
        currentMarker = L.layerGroup([
          L.marker([data.sub_lat, data.sub_lon], {
            icon: L.divIcon({
              className: 'cur-sat-wrap',
              html: '<span class="cur-sat-pulse"></span><span class="cur-sat-dot"></span>',
              iconSize: [18, 18], iconAnchor: [9, 9]
            })
          }),
          L.marker([data.sub_lat, data.sub_lon], {
            icon: L.divIcon({ className: 'sat-label', html: labelText, iconSize: [100, 16] })
          })
        ]).addTo(map);
        // 跟随卫星: 地图中心平滑移动到当前星下点
        if (followSat) map.panTo([data.sub_lat, data.sub_lon], { animate: true, duration: 0.5 });
        // 雷达图当前点
        drawRadar(radarPoints, { az: data.azimuth, el: data.elevation }, radarPass);
      } catch (e) { /* 忽略 */ }
    };
    update();
    infoTimer = setInterval(update, 1000);
  }

  // ===== 过境列表 =====
  async function loadPasses(norad, seq) {
    try {
      // 加载占位提示
      $('passList').innerHTML = '<div style="font-size:12px;color:#64748b;padding:4px 2px">加载过境中…</div>';
      const res = await fetch('/api/sat/passes/' + norad + '?hours=24&min_elev=0');
      const data = await res.json();
      if (seq !== undefined && seq !== selectSeq) return;  // 已切换, 丢弃旧响应
      if (!data.ok) return;
      const list = $('passList');
      list.innerHTML = '<div style="font-size:13px;color:#7dd3fc;margin-bottom:8px">未来过境</div>';
      data.passes.slice(0, 5).forEach(p => {
        const aos = new Date(p.aos * 1000);
        const los = new Date(p.los * 1000);
        const fmt = d => d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
        list.innerHTML += `<div class="pass-item">
          <div>${fmt(aos)} - ${fmt(los)}</div>
          <div class="t">最大仰角 ${p.max_el}° · 方位 ${p.aos_az}°→${p.los_az}° · ${Math.round(p.duration / 60)}分</div></div>`;
      });
    } catch (e) { /* 忽略 */ }
  }

  // ===== 雷达图 =====
  async function loadRadar(norad, seq) {
    try {
      const res = await fetch('/api/sat/radar/' + norad);
      const data = await res.json();
      if (seq !== undefined && seq !== selectSeq) return;  // 已切换, 丢弃旧响应
      if (!data.ok) return;
      radarPoints = data.points;
      radarPass = data.pass;
      drawRadar(radarPoints, null, radarPass);
    } catch (e) { /* 忽略 */ }
  }

  function drawRadar(points, current, passInfo) {
    const canvas = $('radar');
    const ctx = canvas.getContext('2d');
    const W = canvas.width, H = canvas.height;
    const cx = W / 2, cy = H / 2, R = Math.min(W, H) / 2 - 12;
    ctx.clearRect(0, 0, W, H);
    // 网格
    ctx.strokeStyle = '#334155';
    ctx.lineWidth = 1;
    [1 / 3, 2 / 3, 1].forEach(f => {
      ctx.beginPath();
      ctx.arc(cx, cy, R * f, 0, Math.PI * 2);
      ctx.stroke();
    });
    ctx.beginPath();
    ctx.moveTo(cx - R, cy); ctx.lineTo(cx + R, cy);
    ctx.moveTo(cx, cy - R); ctx.lineTo(cx, cy + R);
    ctx.stroke();
    // 方位标注
    ctx.fillStyle = '#94a3b8';
    ctx.font = '11px sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText('N', cx, cy - R - 4);
    ctx.fillText('E', cx + R + 10, cy + 4);
    ctx.fillText('S', cx, cy + R + 14);
    ctx.fillText('W', cx - R - 10, cy + 4);
    // 轨迹
    if (points && points.length > 1) {
      ctx.strokeStyle = '#facc15';
      ctx.lineWidth = 2;
      ctx.beginPath();
      let started = false;
      points.forEach(p => {
        if (p.el < 0) { started = false; return; }
        const r = R * (1 - p.el / 90);
        const x = cx + r * Math.sin(p.az * Math.PI / 180);
        const y = cy - r * Math.cos(p.az * Math.PI / 180);
        if (!started) { ctx.moveTo(x, y); started = true; }
        else ctx.lineTo(x, y);
      });
      ctx.stroke();
    }
    // 当前卫星位置
    if (current) {
      const r = R * (1 - current.el / 90);
      const x = cx + r * Math.sin(current.az * Math.PI / 180);
      const y = cy - r * Math.cos(current.az * Math.PI / 180);
      ctx.fillStyle = '#0ea5e9';
      ctx.beginPath();
      ctx.arc(x, y, 5, 0, Math.PI * 2);
      ctx.fill();
    }
    // 过境 AOS 起点标记 (外圈, el≈0)
    if (passInfo && passInfo.aos_az !== undefined) {
      const x = cx + R * Math.sin(passInfo.aos_az * Math.PI / 180);
      const y = cy - R * Math.cos(passInfo.aos_az * Math.PI / 180);
      ctx.fillStyle = '#22c55e';
      ctx.beginPath();
      ctx.arc(x, y, 5, 0, Math.PI * 2);
      ctx.fill();
      ctx.font = '11px sans-serif';
      ctx.textAlign = 'center';
      ctx.fillText('AOS', x, y - 10);
    }
  }

  // ===== AS5600 实时标定面板 UI =====
  function formatPan(v) {
    if (v === null || v === undefined) return '-';
    // pan 0~360°: 360° 与 0° 同位置 (光电零位), 显示为 0°
    let val = v;
    if (val >= 359.95) val = 0.0;
    return val.toFixed(2);
  }

  function updateEncCalUi(d) {
    const latest = d || {};
    const set = (id, v) => { const el = $(id); if (el) el.textContent = v; };
    set('encCalRaw', latest.raw ?? '-');
    // angle: ESP32 回传原始累积角 (与控制组件下方 ang 一致), cont_angle 是除以 4 的物理连续角
    set('encCalCont', latest.angle ?? '-');
    set('encCalPan', formatPan(latest.pan));
    set('encCalRpd', latest.zero_angle !== undefined && latest.zero_angle !== null
      ? Number(latest.zero_angle).toFixed(2) + '°' : '-');
  }

  async function loadEncCal() {
    try {
      const r = await fetch('/api/encoder');
      const d = await r.json();
      if (d.ok) updateEncCalUi(d);
    } catch (e) { /* 忽略 */ }
  }

  async function pollEncoderCal() {
    try {
      const r = await fetch('/api/encoder');
      const d = await r.json();
      if (d.ok) updateEncCalUi(d);
    } catch (e) { /* 忽略 */ }
  }

  $('encCalToggle').onclick = () => {
    $('encCalBody').classList.toggle('hidden');
    $('encCalToggle').classList.toggle('collapsed');
  };

  // ===== 光电自动校零 + 测速 =====
  async function pollPhotoCalib() {
    try {
      const r = await fetch('/api/encoder/photocalib');
      const d = await r.json();
      if (!d.ok) return;
      const st = $('encPhotoStatus');
      if (!st) return;
      const modeName = '自动校零';
      if (d.running) {
        const phase = d.phase === 'waiting_first' ? '等待光电触发起点…' :
                      d.phase === 'waiting_second' ? '已触发起点，转一圈中，等待终点…' : '处理中…';
        st.innerHTML = `⏳ 光电${modeName}中：${phase}`;
      } else if (d.result && d.result.ok) {
        const revs = d.result.as5600_revs;
        const ratioTxt = (revs !== null && revs !== undefined) ? `${revs.toFixed(2)}:1` : '-';
        st.innerHTML = `✅ 光电${modeName}完成：传动比 <b>${ratioTxt}</b>（AS5600 转角 <b>${d.result.angle_delta?.toFixed(1) ?? '-'}°</b>，0 基准角 <b>${d.result.zero_angle?.toFixed(1) ?? '-'}°</b>）` +
          (d.result.pan_speed_dps ? `，水平速度已校准 <b>${d.result.pan_speed_dps.toFixed(3)}</b>°/s` : '') +
          `（一圈耗时 ${d.result.elapsed}s）`;
        loadEncCal();
      } else if (d.result && !d.result.ok) {
        st.innerHTML = `❌ ${d.result.detail || '光电' + modeName + '失败'}`;
      } else {
        st.innerHTML = '';
      }
    } catch (e) { /* 忽略 */ }
  }

  async function startPhotoCalib() {
    if (!confirm('将让云台自动转一圈，利用光电传感器自动完成校零与测速，确定？')) return;
    try {
      const r = await fetch('/api/encoder/photocalib', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'start' })
      });
      const d = await r.json();
      if (d.ok) { toast(d.msg || '已开始'); pollPhotoCalib(); }
      else toast(d.detail || '启动失败');
    } catch (e) { toast('启动失败: ' + e.message); }
  }

  async function manualSetZero() {
    if (!confirm('将当前回传角度设为物理 0° 基准角 (zero_angle)，确定？')) return;
    try {
      const r = await fetch('/api/encoder/setzero', { method: 'POST' });
      const d = await r.json();
      if (d.ok) { toast(d.msg || '已设置'); loadEncCal(); }
      else toast(d.detail || '设置失败');
    } catch (e) { toast('设置失败: ' + e.message); }
  }

  $('btnEncPhoto').onclick = () => startPhotoCalib();
  $('btnEncSetZero').onclick = () => manualSetZero();

  setInterval(pollPhotoCalib, 1000);

  // ===== 云台跟踪 =====
  $('btnTrack').onclick = async () => {
    if (tracking) {
      // 正在跟踪 (卫星或月球): 一律停止, 无需 currentNorad
      try {
        await fetch('/api/sat/track/stop', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
        const targetLabel = { moon: '月球', sun: '太阳' }[trackingTarget];
        trackingTarget = 'sat';
        setTrackingUI(false);
        stopMoonInfo();
        toast(targetLabel ? `已停止${targetLabel}跟踪` : '已停止跟踪');
      } catch (e) { toast('停止失败'); }
      return;
    }
    if (!currentNorad) { toast('请先选择卫星'); return; }
    try {
      const res = await fetch('/api/sat/track/' + currentNorad, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
      const data = await res.json();
      if (data.ok && data.tracking) {
        trackingTarget = 'sat';
        stopMoonInfo();
        setTrackingUI(true);
        toast('开始跟踪 ' + currentNorad);
      } else if (data.ok && data.detail) {
        // 俯仰归零中: 后台正在开环把俯仰降到 0°, 完成后自动开始跟踪
        trackingTarget = 'sat';
        stopMoonInfo();
        setTrackingUI(true);
        toast(data.detail);
      } else toast('跟踪失败: ' + (data.detail || ''));
    } catch (e) { toast('跟踪失败'); }
  };

  // ===== 月球/太阳跟踪 (通用天体) =====
  let trackingTarget = 'sat';   // 'sat' | 'moon' | 'sun'
  let moonTimer = null;

  function stopMoonInfo() { if (moonTimer) { clearInterval(moonTimer); moonTimer = null; } }

  function setTrackingUI(on, label) {
    tracking = on;
    $('btnTrack').textContent = on ? (label || '停止跟踪') : '开始跟踪';
    $('btnTrack').classList.toggle('tracking', on);
    $('btnMoonTrack').classList.toggle('active', on && trackingTarget === 'moon');
    $('btnSunTrack').classList.toggle('active', on && trackingTarget === 'sun');
  }

  const CELESTIAL = {
    moon: { emoji: '🌙', name: '月球', endpoint: '/api/moon/position' },
    sun:  { emoji: '☀️', name: '太阳', endpoint: '/api/sun/position' },
  };

  async function pollCelestialInfo(target) {
    try {
      const c = CELESTIAL[target];
      const r = await fetch(c.endpoint);
      const d = await r.json();
      if (!d.ok) return;
      $('satInfo').innerHTML =
        `<div class="row"><span class="k">目标</span><span class="v">${c.emoji} ${c.name}</span></div>` +
        `<div class="row"><span class="k">方位角</span><span class="v">${d.azimuth}°</span></div>` +
        `<div class="row"><span class="k">仰角</span><span class="v">${d.elevation}°</span></div>` +
        `<div class="row"><span class="k">距离</span><span class="v">${Math.round(d.distance)} km</span></div>` +
        `<div class="row"><span class="k">状态</span><span class="v" style="color:${d.visible ? '#4ade80' : '#facc15'}">${d.visible ? '地平线上' : '地平线下'}</span></div>`;
    } catch (e) { /* 忽略 */ }
  }

  async function startCelestialTracking(target) {
    try {
      const c = CELESTIAL[target];
      const res = await fetch('/api/sat/track/' + target, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
      const data = await res.json();
      if (!data.ok) { toast(c.name + '跟踪失败: ' + (data.detail || '')); return; }
      trackingTarget = target;
      setTrackingUI(true, `停止跟踪 ${c.emoji}`);
      stopMoonInfo();
      pollCelestialInfo(target);
      moonTimer = setInterval(() => pollCelestialInfo(target), 2000);
      toast(data.detail || `开始${c.name}跟踪`);
    } catch (e) { toast(c.name + '跟踪失败'); }
  }

  function bindCelestialButton(btnId, target) {
    $(btnId).onclick = () => {
      if (tracking && trackingTarget === target) {
        // 已在跟踪该天体 -> 停止
        fetch('/api/sat/track/stop', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
        trackingTarget = 'sat';
        setTrackingUI(false);
        stopMoonInfo();
        toast('已停止' + CELESTIAL[target].name + '跟踪');
      } else {
        startCelestialTracking(target);
      }
    };
  }
  bindCelestialButton('btnMoonTrack', 'moon');
  bindCelestialButton('btnSunTrack', 'sun');

  // ===== 刷新后恢复跟踪状态 (后端线程仍在跟踪) =====
  let pendingRestoreNorad = null;  // 跟踪中的卫星, 待收藏列表渲染后恢复选中视图
  async function restoreTracking() {
    try {
      const r = await fetch('/api/sat/track/status');
      const d = await r.json();
      if (!d.ok) return false;
      if (d.tracking && CELESTIAL[d.norad]) {
        // 恢复月球/太阳跟踪状态
        const c = CELESTIAL[d.norad];
        trackingTarget = d.norad;
        setTrackingUI(true, `停止跟踪 ${c.emoji}`);
        stopMoonInfo();
        pollCelestialInfo(d.norad);
        moonTimer = setInterval(() => pollCelestialInfo(d.norad), 2000);
        toast(c.name + '跟踪继续进行中');
        return true;  // 天体跟踪, 不自动选卫星
      }
      if (d.tracking && d.norad) {
        tracking = true;
        $('btnTrack').textContent = '停止跟踪';
        $('btnTrack').classList.add('tracking');
        // 记录待恢复卫星, 收藏列表渲染完成后自动选中 (原实现遍历空 DOM 无法恢复)
        pendingRestoreNorad = d.norad;
        toast('跟踪继续进行中 (' + d.norad + ')');
        return true;
      }
      return false;
    } catch (e) { return false; }
  }

  // ===== 初始化 =====
  initMap();
  bindTrackDurBtns();
  loadEncCal();
  // 先确认后端跟踪状态 (决定恢复哪颗卫星), 再渲染收藏列表并恢复选中
  (async () => {
    await restoreTracking();
    loadFavorites();
  })();
  setInterval(loadFavorites, 30000);
  setInterval(pollEncoderCal, 500);

  // ===== 地图云台方向指示线（IIFE 闭包内，直接访问 map / obsMarker） =====
  let ptzDirLine = null, ptzArrow = null;
  function ptzDirEnd(lat, lon, bearingDeg, distKm) {
    const R = 6371, brng = bearingDeg * Math.PI / 180;
    const d = distKm / R, r1 = lat * Math.PI / 180, r2 = lon * Math.PI / 180;
    const lt = Math.asin(Math.sin(r1) * Math.cos(d) + Math.cos(r1) * Math.sin(d) * Math.cos(brng));
    const ln = r2 + Math.atan2(Math.sin(brng) * Math.sin(d) * Math.cos(r1), Math.cos(d) - Math.sin(r1) * Math.sin(lt));
    return [lt * 180 / Math.PI, ln * 180 / Math.PI];
  }
  window.updatePtzDir = function(panDeg) {
    if (!map || !obsMarker || isNaN(panDeg)) { console.log('ptzDir skip', {obsMarker, map, panDeg}); return; }
    const pos = obsMarker.getLatLng(), o = [pos.lat, pos.lng];
    const end = ptzDirEnd(o[0], o[1], panDeg, 1500);
    const back = ptzDirEnd(end[0], end[1], (panDeg + 180) % 360, 30);
    const l1 = ptzDirEnd(back[0], back[1], (panDeg + 270) % 360, 15);
    const l2 = ptzDirEnd(back[0], back[1], (panDeg + 90) % 360, 15);
    if (!ptzDirLine) {
      console.log('ptzDir create', {obs: o, end, panDeg});
      ptzDirLine = L.polyline([o, end], { color: '#ff4444', weight: 5, opacity: 1 }).addTo(map);
      ptzArrow = L.polygon([end, l1, l2], { color: '#ff4444', fillColor: '#ff4444', fillOpacity: 1, weight: 0 }).addTo(map);
    } else {
      ptzDirLine.setLatLngs([o, end]);
      ptzArrow.setLatLngs([end, l1, l2]);
    }
    ptzDirLine.bringToFront();
    ptzArrow.bringToFront();
  };
})();
