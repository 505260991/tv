import os
import requests
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from flask import Flask, request, jsonify, render_template

# ================= 配置区 =================
DOWNLOAD_DIR = "/storage/emulated/0/tvb字幕下载/"
MAX_WORKERS = 5 # 并发线程数，5-8是最佳甜点位，太高容易被 115 封IP
# ==========================================

RED = "\033[91m"
GREEN = "\033[92m"
CYAN = "\033[96m"
YELLOW = "\033[93m"
PURPLE = "\033[95m"
BOLD = "\033[1m"
RESET = "\033[0m"

def get_cookie_from_file():
    cookie_file = "115cookie.txt"
    if os.path.exists(cookie_file):
        with open(cookie_file, "r", encoding="utf-8") as f:
            return f.read().strip() 
    return ""

COOKIES = get_cookie_from_file()

session = requests.Session()
# V7.0 性能优化：扩大底层连接池
adapter = HTTPAdapter(pool_connections=50, pool_maxsize=50, max_retries=Retry(total=3, backoff_factor=0.5))
session.mount("http://", adapter)
session.mount("https://", adapter)
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Cookie": COOKIES,
    "Referer": "https://115.com/"
})

def get_smart_info(cid):
    folders, files = [], []
    offset = 0
    limit = 500
    blacklisted_exts = ('.zip', '.rar', '.7z', '.tar', '.gz', '.iso')
    while True:
        api_url = f"https://webapi.115.com/files?aid=1&cid={cid}&o=file_name&asc=1&offset={offset}&show_dir=1&limit={limit}"
        try:
            data = session.get(api_url, timeout=10).json()
            if not data.get("state"): break
            items = data.get("data", [])
            if not items: break
            for item in items:
                if item.get("fid") and item.get("pc"):
                    if not item.get("n", "").lower().endswith(blacklisted_exts):
                        files.append({"name": item.get("n"), "pc": item.get("pc"), "fid": str(item.get("fid"))})
                else:
                    f_cid = item.get("cid") or item.get("cate_id") or ""
                    if f_cid: folders.append({"name": item.get("n"), "cid": str(f_cid)})
            if len(items) < limit: break
            offset += limit
        except: break
    return folders, files

def search_115_files(keyword):
    folders, files = [], []
    api_url = f"https://webapi.115.com/files/search?search_value={keyword}&offset=0&limit=100"
    blacklisted_exts = ('.zip', '.rar', '.7z', '.tar', '.gz', '.iso')
    try:
        data = session.get(api_url, timeout=10).json()
        if data.get("state"):
            items = data.get("data", [])
            for item in items:
                if item.get("fid") and item.get("pc"):
                    if not item.get("n", "").lower().endswith(blacklisted_exts):
                        files.append({"name": item.get("n"), "pc": item.get("pc"), "fid": str(item.get("fid"))})
                else:
                    f_cid = item.get("cid") or item.get("cate_id") or ""
                    if f_cid: folders.append({"name": item.get("n"), "cid": str(f_cid)})
    except: pass
    return folders, files

def rename_115_file(fid, new_name):
    url = "https://webapi.115.com/files/edit"
    try:
        res = session.post(url, data={"fid": fid, "file_name": new_name}, timeout=10).json()
        return res.get("state", False)
    except: return False

def delete_115_file(fid, pid):
    url = "https://webapi.115.com/rb/delete"
    data = {"fid[0]": fid, "pid": pid}
    try:
        res = session.post(url, data=data, timeout=10).json()
        return res.get("state", False)
    except: return False

def create_115_folder(pid, cname):
    url = "https://webapi.115.com/files/add"
    data = {"pid": pid, "cname": cname}
    try:
        res = session.post(url, data=data, timeout=10).json()
        if res.get("state"): return str(res.get("cid"))
    except: pass
    return None

def move_115_file(fid, target_pid):
    url = "https://webapi.115.com/files/move"
    data = {"pid": target_pid, "fid[0]": fid}
    try:
        res = session.post(url, data=data, timeout=10).json()
        return res.get("state", False)
    except: return False

def download_single_subtitle(name, pickcode, save_dir):
    base_name = os.path.splitext(name)[0]
    save_path = os.path.join(save_dir, f"{base_name}.srt")
    if os.path.exists(save_path) and os.path.getsize(save_path) > 50:
        return True, "skipped"
    api_url = f"https://webapi.115.com/movies/subtitle?pickcode={pickcode}"
    try:
        response = session.get(api_url, timeout=10)
        data = response.json()
        if data.get("state") and data.get("data", {}).get("list"):
            srt_url = data["data"]["list"][0].get("url")
            if srt_url:
                srt_res = session.get(srt_url, timeout=10)
                content = srt_res.content
                if len(content) < 50 or b"<html" in content[:200].lower(): return False, "failed"
                with open(save_path, "wb") as f: f.write(content)
                return True, "downloaded"
        return False, "failed"
    except: return False, "failed"

def get_ep_num(name):
    try:
        base_name, _ = os.path.splitext(name)
        clean = re.sub(r'(1080[pi]|720[pi]|2160[pi]|4k|2k|h\.?264|h\.?265|x\.?264|x\.?265|hevc|web-dl|aac|ac3|\d{4})', '', base_name, flags=re.IGNORECASE)
        match = re.search(r'(?i)(?:ep|e|第)\s*(\d{1,4})(?:集|话|話)?', clean)
        if match: return int(match.group(1))
        nums = re.findall(r'(?:^|[. \-_\[【(])(\d{1,4})(?:[. \-_\]】)]|$)', clean)
        if nums: return int(nums[-1])
        nums = re.findall(r'\d+', clean)
        return int(nums[-1]) if nums else -1
    except: return -1

def format_ep_ranges(eps):
    if not eps: return ""
    eps = sorted(list(set(eps)))
    ranges = []
    start = prev = eps[0]
    for ep in eps[1:]:
        if ep == prev + 1: prev = ep
        else:
            if start == prev: ranges.append(str(start))
            elif start == prev - 1: ranges.append(f"{start}, {prev}")
            else: ranges.append(f"{start}-{prev}")
            start = prev = ep
    if start == prev: ranges.append(str(start))
    elif start == prev - 1: ranges.append(f"{start}, {prev}")
    else: ranges.append(f"{start}-{prev}")
    return ", ".join(ranges)

def generate_missing_hint(names_list):
    fail_eps, fail_others = [], []
    for n in names_list:
        ep = get_ep_num(n)
        if ep != -1: fail_eps.append(ep)
        else: fail_others.append(n)
    hints = []
    if fail_eps: hints.append(f"第 {format_ep_ranges(fail_eps)} 集")
    if fail_others:
        others_str = ', '.join([f"'{n}'" for n in fail_others[:2]])
        if len(fail_others) > 2: others_str += f" 等 {len(fail_others)} 个文件"
        hints.append(others_str)
    return " / ".join(hints)

# ================= V7.0 多线程核武：洗盘收纳提速 500% =================
def web_clean_folder(cid):
    folders, files = get_smart_info(cid)
    if not files: return {"status": "error", "msg": "目录为空，无可操作文件！"}
    
    backup_folder_name = "[备份]多余字幕"
    backup_cid = None
    for f in folders:
        if f["name"] == backup_folder_name:
            backup_cid = f["cid"]
            break
            
    # 任务队列
    delete_tasks = []
    move_tasks = []
    rename_tasks = []

    for f in files:
        if f["name"].lower().endswith(('.jpg', '.png', '.url', '.txt', '.html')):
            move_tasks.append(f["fid"])

    ep_map = {}
    for f in files:
        ep = get_ep_num(f["name"])
        if ep != -1:
            if ep not in ep_map: ep_map[ep] = {'vids': [], 'subs': []}
            if f["name"].lower().endswith(('.srt', '.ass', '.ssa', '.vtt')):
                ep_map[ep]['subs'].append(f)
            else:
                ep_map[ep]['vids'].append(f)
                
    for ep, group in ep_map.items():
        if len(group['vids']) == 1:
            vid = group['vids'][0]
            ext = os.path.splitext(vid["name"])[1]
            new_vid_name = f"{ep:02d}{ext}" 
            if vid["name"] != new_vid_name:
                rename_tasks.append((vid["fid"], new_vid_name))
                
        if len(group['subs']) > 0:
            subs = group['subs']
            kept_sub = None
            
            for s in subs:
                if '_sc' in s["name"].lower() or '简' in s["name"]: kept_sub = s; break
            if not kept_sub:
                for s in subs:
                    if '_tc' in s["name"].lower() or '繁' in s["name"]: kept_sub = s; break
            if not kept_sub:
                for s in subs:
                    if '_en' not in s["name"].lower() and 'eng' not in s["name"].lower(): kept_sub = s; break
            if not kept_sub:
                kept_sub = subs[0]
                
            for s in subs:
                if s["fid"] != kept_sub["fid"]:
                    move_tasks.append(s["fid"])
                    
            sub_ext = os.path.splitext(kept_sub["name"])[1]
            new_sub_name = f"{ep:02d}{sub_ext}"
            if kept_sub["name"] != new_sub_name:
                rename_tasks.append((kept_sub["fid"], new_sub_name))

    moved_count, renamed_count, deleted_count = 0, 0, 0

    # 建房逻辑（必须串行，因为要拿到 cid）
    if move_tasks and not backup_cid:
        backup_cid = create_115_folder(cid, backup_folder_name)

    # 🚀 启动多线程狂暴模式！
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        # 1. 并发移动垃圾
        if backup_cid and move_tasks:
            mov_futures = [executor.submit(move_115_file, fid, backup_cid) for fid in move_tasks]
            moved_count = sum([1 for f in as_completed(mov_futures) if f.result()])
            
        # 2. 并发删除垃圾
        if delete_tasks:
            del_futures = [executor.submit(delete_115_file, fid, cid) for fid in delete_tasks]
            deleted_count = sum([1 for f in as_completed(del_futures) if f.result()])
            
        # 3. 并发重塑命名
        if rename_tasks:
            ren_futures = [executor.submit(rename_115_file, fid, new_name) for fid, new_name in rename_tasks]
            renamed_count = sum([1 for f in as_completed(ren_futures) if f.result()])

    if moved_count == 0 and renamed_count == 0 and deleted_count == 0:
        return {"status": "success", "msg": "✨ 已经是极简格式且无多余文件，无需处理！"}
        
    return {"status": "success", "msg": f"📦 极速多线程收纳！打包 {moved_count} 个多余文件，极简重塑 {renamed_count} 次！"}

# ================= V7.0 多线程核武：常规对齐提速 500% =================
def web_process_folder(cid, folder_name):
    folders, files = get_smart_info(cid)
    if not files: return {"status": "error", "msg": "目录为空或仅包含子文件夹。"}
    sub_exts = ('.srt', '.ass', '.ssa', '.vtt')
    subs = [f for f in files if f["name"].lower().endswith(sub_exts)]
    vids = [f for f in files if not f["name"].lower().endswith(sub_exts)]
    
    if not subs:
        if not vids: return {"status": "error", "msg": "里面没有视频文件！"}
        save_dir = os.path.join(DOWNLOAD_DIR, folder_name)
        os.makedirs(save_dir, exist_ok=True)
        success_count, skip_count, failed_count = 0, 0, 0
        failed_names = []
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(download_single_subtitle, f["name"], f["pc"], save_dir): f["name"] for f in vids}
            for future in as_completed(futures):
                vid_name = futures[future]
                success, status = future.result()
                if success:
                    if status == "skipped": skip_count += 1
                    else: success_count += 1
                else: 
                    failed_count += 1
                    failed_names.append(vid_name)
        if os.path.exists(save_dir) and not os.listdir(save_dir): os.rmdir(save_dir) 
        missing_hint = f" 失败漏缺: {generate_missing_hint(failed_names)}" if failed_names else ""
        return {"status": "success", "msg": f"下载 {success_count} 集, 跳过 {skip_count} 集。{missing_hint}"}
    
    if not vids: return {"status": "error", "msg": "只有字幕没有视频！"}
    rename_tasks = []
    if len(vids) == 1 and len(subs) == 1:
        old_sub = subs[0]["name"]
        new_base = os.path.splitext(vids[0]["name"])[0]
        ext = os.path.splitext(old_sub)[1]
        new_sub = f"{new_base}{ext}"
        if old_sub != new_sub: rename_tasks.append((subs[0]["fid"], new_sub))
    else:
        s_map = {}
        for s in subs:
            ep = get_ep_num(s["name"])
            if ep != -1: s_map[ep] = s 
        unmatched_vids = []
        for v in vids:
            ep = get_ep_num(v["name"])
            if ep != -1 and ep in s_map:
                s = s_map[ep]
                old_sub = s["name"]
                new_base = os.path.splitext(v["name"])[0]
                ext = os.path.splitext(old_sub)[1]
                new_sub = f"{new_base}{ext}"
                if old_sub != new_sub: rename_tasks.append((s["fid"], new_sub))
            else: unmatched_vids.append(v["name"])
        if unmatched_vids:
            missing_hint = f" 失败漏缺: {generate_missing_hint(unmatched_vids)}"
            return {"status": "error", "msg": f"精准匹配拦截：部分视频找不到对应字幕！{missing_hint}"}
            
    if not rename_tasks: return {"status": "success", "msg": "✨ 已完全对齐，无需改动！"}
    
    success_count = 0
    # 🚀 启动多线程并发改名！
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        ren_futures = [executor.submit(rename_115_file, fid, new_name) for fid, new_name in rename_tasks]
        success_count = sum([1 for f in as_completed(ren_futures) if f.result()])
        
    return {"status": "success", "msg": f"☁️ 极速多线程成功改名匹配了 {success_count} 个字幕文件！"}

app = Flask(__name__)

@app.route("/")
def index(): return render_template("index.html")

@app.route('/favicon.ico')
def favicon(): return '', 204  

@app.route("/api/list")
def api_list():
    cid = request.args.get("cid", "0")
    folders, files = get_smart_info(cid)
    return jsonify({"folders": folders, "files": files})

@app.route("/api/search")
def api_search():
    keyword = request.args.get("q", "")
    folders, files = search_115_files(keyword)
    return jsonify({"folders": folders, "files": files})

@app.route("/api/process")
def api_process():
    cid = request.args.get("cid")
    folder_name = request.args.get("name", "未命名目录")
    if not cid: return jsonify({"status": "error", "msg": "缺少 CID 参数"})
    result = web_process_folder(cid, folder_name)
    return jsonify(result)

@app.route("/api/clean")
def api_clean():
    cid = request.args.get("cid")
    if not cid: return jsonify({"status": "error", "msg": "缺少 CID 参数"})
    result = web_clean_folder(cid)
    return jsonify(result)

if __name__ == "__main__":
    if not COOKIES:
        print(f"{RED}[!] 错误：找不到 115cookie.txt！{RESET}")
    else:
        print(f"{GREEN}====================================={RESET}")
        print(f"{BOLD}🌐 115 Web 版车间 V7.0 (V8 多线程狂暴提速版) 启动成功！{RESET}")
        print(f"{CYAN}请在手机/电脑浏览器中打开: {BOLD}http://127.0.0.1:5000{RESET}")
        print(f"{YELLOW}按 CTRL+C 结束运行{RESET}")
        print(f"{GREEN}====================================={RESET}")
        app.run(host="0.0.0.0", port=5000, debug=False)
