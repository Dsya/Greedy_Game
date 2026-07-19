import http.server
import socketserver
import json
import random
import urllib.parse
import socket
import time
import sys
import threading
import os

BGM_FILE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bgm3.mp3")
PORT = 8000

# ==========================================
# GAME STATE CONFIGURATION (RUMMY INDONESIA)
# ==========================================
GAME_STATE = {
    "status": "waiting",  # waiting, playing, game_over
    "players": [],        # list: {id, name, cards[], melds: [], has_sequence: bool, score: 0, left: bool, ready: bool}
    "deck": [],
    "discard_pile": [],   # Tumpukan terbuka (list dari string kartu)
    "current_turn_idx": 0,
    "logs": [],
    "winner_id": None,
    "host_id": None  # id pemain yang berperan sebagai host/pemilik lobby
}

GAME_STATE_LOCK = threading.Lock()

def reassign_host_if_needed():
    """Jika host_id tidak lagi ada di antara pemain (keluar/DC), pindahkan status host
    ke pemain pertama yang tersisa di daftar."""
    global GAME_STATE
    host_still_present = any(p["id"] == GAME_STATE["host_id"] for p in GAME_STATE["players"])
    if not host_still_present:
        if GAME_STATE["players"]:
            new_host = GAME_STATE["players"][0]
            GAME_STATE["host_id"] = new_host["id"]
            GAME_STATE["logs"].append(f"👑 {new_host['name']} kini menjadi host lobby baru.")
        else:
            GAME_STATE["host_id"] = None

def get_card_suit_val(card_str):
    suit, val_str = card_str.split("-")
    val_map = {
        "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7, "8": 8, "9": 9, "10": 10,
        "J": 11, "Q": 12, "K": 13, "A": 14
    }
    return suit, val_map.get(val_str, 0), val_str

def check_is_sequence(cards_list):
    """
    Sequence: Minimal 3 kartu, suit sama.
    Angka saja (2-10) ATAU Gambar saja (J-Q-K). As (14) tidak boleh masuk sequence.
    """
    if len(cards_list) < 3:
        return False
    
    parsed = [get_card_suit_val(c) for c in cards_list]
    suit = parsed[0][0]
    
    # Cek apakah suit sama semua
    if not all(p[0] == suit for p in parsed):
        return False
        
    vals = sorted([p[1] for p in parsed])
    
    # As tidak boleh ada di sequence
    if 14 in vals:
        return False
        
    # Cek apakah berurutan
    for i in range(len(vals) - 1):
        if vals[i+1] - vals[i] != 1:
            return False
            
    # Cek tidak boleh mencampur angka dan gambar (10 tidak berdekatan dengan J)
    # Angka maksimal 10, Gambar minimal 11 (J)
    if vals[0] <= 10 and vals[-1] >= 11:
        return False
        
    return True

def check_is_set(cards_list):
    """Set: 3 atau 4 kartu dengan rank/nilai yang sama."""
    if len(cards_list) < 3 or len(cards_list) > 4:
        return False
    parsed = [get_card_suit_val(c) for c in cards_list]
    rank = parsed[0][2]
    return all(p[2] == rank for p in parsed)

def calculate_card_score(card_str):
    _, val, rank = get_card_suit_val(card_str)
    if rank == "A":
        return 15
    elif rank in ["J", "Q", "K"]:
        return 10
    else:
        return 5

def count_hand_score(player):
    """Menghitung total skor saat ini berdasarkan meld (+), sisa tangan (-)"""
    score = 0
    # Hitung Melds Positif
    for meld in player["melds"]:
        for c in meld:
            score += calculate_card_score(c)
            
    # Hitung Tangan Negatif
    for c in player["cards"]:
        score -= calculate_card_score(c)
    return score

def greedy_suggest(cards):
    """
    ALGORITMA GREEDY untuk menganalisis kartu di tangan pemain.

    Prinsip greedy: di setiap langkah, ambil keputusan lokal yang paling
    "optimal saat itu juga" tanpa mundur (backtrack):
      1. Cari SET dulu -> proses rank yang paling BANYAK duplikatnya duluan
         (peluang jadi paling besar diambil paling awal).
      2. Dari sisa kartu, cari SEQUENCE per suit secara berurutan (scan kiri
         ke kanan, begitu nemu 3+ kartu berurutan langsung diambil sebagai
         kombinasi, tidak dicoba kombinasi susunan lain / tidak backtrack).
      3. Sisa kartu yang tidak masuk kombinasi apa pun dihitung "skor
         keterhubungan" (peluang menjadi kombinasi di masa depan). Kartu
         dengan skor paling rendah = paling kesepian = disarankan dibuang.
    Mengembalikan (groups, discard_suggestion).
    """
    from collections import defaultdict

    remaining = list(cards)
    groups = []

    # STEP 1: greedy ambil SET, mulai dari rank paling banyak duplikatnya
    rank_map = defaultdict(list)
    for c in remaining:
        _, _, rank = get_card_suit_val(c)
        rank_map[rank].append(c)

    for rank, group in sorted(rank_map.items(), key=lambda kv: -len(kv[1])):
        if len(group) >= 3:
            take = group[:4]
            groups.append({"type": "set", "cards": take, "ready": True})
            for c in take:
                remaining.remove(c)

    # STEP 2: greedy ambil SEQUENCE per suit dari sisa kartu (As tidak ikut)
    suit_map = defaultdict(list)
    for c in remaining:
        suit, val, rank = get_card_suit_val(c)
        if rank != "A":
            suit_map[suit].append((val, c))

    for suit, group in suit_map.items():
        group.sort()
        i = 0
        while i < len(group):
            run = [group[i]]
            j = i + 1
            while j < len(group) and group[j][0] == run[-1][0] + 1:
                # jangan lompati batas angka(<=10) vs gambar(>=11)
                if run[-1][0] <= 10 and group[j][0] >= 11:
                    break
                run.append(group[j])
                j += 1
            if len(run) >= 3:
                take = [c for _, c in run]
                groups.append({"type": "sequence", "cards": take, "ready": True})
                for c in take:
                    remaining.remove(c)
                i = j
            else:
                i += 1

    # STEP 3: sisa kartu -> hitung skor keterhubungan utk cari kandidat buang
    def connectivity_score(card):
        suit, val, rank = get_card_suit_val(card)
        score = 0
        for c2 in cards:
            if c2 == card:
                continue
            s2, v2, r2 = get_card_suit_val(c2)
            if r2 == rank:
                score += 2  # berpotensi jadi SET
            if s2 == suit and rank != "A" and r2 != "A" and abs(v2 - val) <= 2:
                score += 1  # berpotensi jadi SEQUENCE

        return score

    discard_suggestion = None
    if remaining:
        groups.append({"type": "floating", "cards": list(remaining), "ready": False})
        scored = [(connectivity_score(c), calculate_card_score(c), c) for c in remaining]
        # greedy: skor keterhubungan paling rendah duluan;
        # kalau seri, prioritaskan buang kartu berpoin lebih besar
        scored.sort(key=lambda x: (x[0], -x[1]))
        discard_suggestion = scored[0][2]

    return groups, discard_suggestion

def advance_turn():
    global GAME_STATE
    idx = GAME_STATE["current_turn_idx"]
    for i in range(1, len(GAME_STATE["players"]) + 1):
        next_idx = (idx + i) % len(GAME_STATE["players"])
        if not GAME_STATE["players"][next_idx].get("left", False):
            GAME_STATE["current_turn_idx"] = next_idx
            return

def end_the_game(winner_id, closing_card=None):
    global GAME_STATE
    GAME_STATE["status"] = "game_over"
    GAME_STATE["winner_id"] = winner_id
    
    # Hitung skor akhir semua pemain
    for p in GAME_STATE["players"]:
        p["score"] = count_hand_score(p)
        if p["id"] == winner_id and closing_card:
            bonus = calculate_card_score(closing_card) * 10
            p["score"] += bonus
            GAME_STATE["logs"].append(f"🏆 {p['name']} MENANG REMI! Bonus Tutup Kartu ({closing_card}): +{bonus} Poin.")
        else:
            GAME_STATE["logs"].append(f"📋 Skor Akhir {p['name']}: {p['score']} Poin.")

def handle_player_leave(player_id, reason="manual"):
    global GAME_STATE
    player = next((p for p in GAME_STATE["players"] if p["id"] == player_id), None)
    if not player:
        return False
        
    name = player["name"]
    if GAME_STATE["status"] == "waiting":
        GAME_STATE["players"] = [p for p in GAME_STATE["players"] if p["id"] != player_id]
        GAME_STATE["logs"].append(f"🔌 {name} keluar dari lobby.")
        reassign_host_if_needed()
        return True
    elif GAME_STATE["status"] == "playing":
        if player.get("left", False):
            return True
        player["left"] = True
        GAME_STATE["logs"].append(f"🔌 {name} terputus dari permainan.")
        
        active_players = [p for p in GAME_STATE["players"] if not p.get("left", False)]
        if len(active_players) <= 1:
            end_the_game(active_players[0]["id"] if active_players else None)
        elif GAME_STATE["players"][GAME_STATE["current_turn_idx"]]["id"] == player_id:
            advance_turn()
        reassign_host_if_needed()
        return True
    return False

def check_and_handle_disconnects():
    global GAME_STATE
    now = time.time()
    for p in GAME_STATE["players"]:
        if not p.get("left", False) and (now - p.get("last_seen", now)) > 15.0:
            handle_player_leave(p["id"], reason="timeout")

def start_game():
    global GAME_STATE
    suits = ["S", "H", "D", "C"]
    values = ["2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A"]
    deck = [f"{s}-{v}" for s in suits for v in values]
    random.shuffle(deck)
    
    for p in GAME_STATE["players"]:
        p["cards"] = []
        p["melds"] = []
        p["has_sequence"] = False
        p["score"] = 0
        p["left"] = False
        p["last_seen"] = time.time()
        p["has_drawn"] = False
        
    # Bagikan 7 kartu ke setiap pemain
    for _ in range(7):
        for p in GAME_STATE["players"]:
            p["cards"].append(deck.pop())
            
    GAME_STATE["deck"] = deck
    # Taruh 1 kartu pertama di tumpukan buangan
    GAME_STATE["discard_pile"] = [deck.pop()]
    GAME_STATE["current_turn_idx"] = 0
    GAME_STATE["status"] = "playing"
    GAME_STATE["winner_id"] = None
    GAME_STATE["logs"] = ["🃏 Game Remi Dimulai! Masing-masing pemain memegang 7 kartu.", f"👉 Giliran pertama: {GAME_STATE['players'][0]['name']}"]

def get_client_state(player_id):
    global GAME_STATE
    players_list = []
    your_cards = []
    your_melds = []
    has_seq = False
    
    for p in GAME_STATE["players"]:
        if p["id"] == player_id:
            your_cards = p["cards"]
            your_melds = p["melds"]
            has_seq = p["has_sequence"]
        players_list.append({
            "id": p["id"],
            "name": p["name"],
            "card_count": len(p["cards"]),
            "melds": p["melds"],
            "has_sequence": p["has_sequence"],
            "score": p.get("score", 0),
            "left": p.get("left", False),
            "ready": p.get("ready", False),
            "is_host": p["id"] == GAME_STATE["host_id"]
        })
        
    current_turn_id = GAME_STATE["players"][GAME_STATE["current_turn_idx"]]["id"] if GAME_STATE["status"] == "playing" else None
    current_turn_name = GAME_STATE["players"][GAME_STATE["current_turn_idx"]]["name"] if GAME_STATE["status"] == "playing" else ""
    
    return {
        "status": GAME_STATE["status"],
        "host_id": GAME_STATE["host_id"],
        "players": players_list,
        "your_cards": your_cards,
        "your_melds": your_melds,
        "has_sequence": has_seq,
        "current_turn": current_turn_id,
        "current_turn_name": current_turn_name,
        "deck_count": len(GAME_STATE["deck"]),
        "discard_pile": GAME_STATE["discard_pile"],
        "winner_id": GAME_STATE["winner_id"],
        "logs": GAME_STATE["logs"][-15:]
    }

# ==========================================
# SERVER REQUEST HANDLER
# ==========================================
class GameRequestHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args): return

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def do_GET(self):
        parsed_path = urllib.parse.urlparse(self.path)
        path = parsed_path.path
        query = urllib.parse.parse_qs(parsed_path.query)
        
        if path == "/api/server_info":
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({"ip": get_local_ip(), "port": PORT}).encode('utf-8'))
            return

        if path == "/api/status":
            player_id = query.get("player_id", [None])[0]
            with GAME_STATE_LOCK:
                if player_id:
                    for p in GAME_STATE["players"]:
                        if p["id"] == player_id: p["last_seen"] = time.time()
                check_and_handle_disconnects()
                state = get_client_state(player_id)
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps(state).encode('utf-8'))
            return
            
        if path in ["/", "/index.html"]:
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(HTML_CONTENT.encode('utf-8'))
            return

        if path == "/bgm3.mp3":
            try:
                with open(BGM_FILE_PATH, "rb") as f:
                    audio_bytes = f.read()
                self.send_response(200)
                self.send_header('Content-Type', 'audio/mpeg')
                self.send_header('Content-Length', str(len(audio_bytes)))
                self.send_header('Accept-Ranges', 'bytes')
                self.send_header('Cache-Control', 'public, max-age=86400')
                self.end_headers()
                self.wfile.write(audio_bytes)
            except FileNotFoundError:
                self.send_error(404, "bgm3.mp3 tidak ditemukan di samping greedy_server.py")
            return

        self.send_error(404)

    def do_POST(self):
        parsed_path = urllib.parse.urlparse(self.path)
        path = parsed_path.path
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length).decode('utf-8') if content_length > 0 else ""
        try: data = json.loads(body)
        except: data = {}
        
        response_data = {"success": False, "error": "Invalid API Action"}
        status_code = 400
        global GAME_STATE
        
        with GAME_STATE_LOCK:
            if path == "/api/join":
                name = data.get("name", "").strip()
                if not name: response_data = {"error": "Nama kosong"}
                elif GAME_STATE["status"] != "waiting": response_data = {"error": "Game sedang berjalan"}
                elif len(GAME_STATE["players"]) >= 4: response_data = {"error": "Lobby penuh (Max 4 Pemain Rummy)"}
                else:
                    player_id = str(random.randint(100000, 999999))
                    GAME_STATE["players"].append({
                        "id": player_id, "name": name, "cards": [], "melds": [],
                        "has_sequence": False, "score": 0, "last_seen": time.time(), "left": False,
                        "ready": False
                    })
                    is_first_player = GAME_STATE["host_id"] is None
                    if is_first_player:
                        GAME_STATE["host_id"] = player_id
                    GAME_STATE["logs"].append(f"👋 {name} masuk ke ruang tunggu." + (" (Host)" if is_first_player else ""))
                    response_data = {"success": True, "player_id": player_id, "player_name": name, "is_host": is_first_player}
                    status_code = 200
                    
            elif path == "/api/start":
                player_id = data.get("player_id")
                if player_id != GAME_STATE["host_id"]:
                    response_data = {"error": "Hanya host yang bisa memulai permainan"}
                elif len(GAME_STATE["players"]) < 2: response_data = {"error": "Butuh minimal 2 pemain"}
                else:
                    start_game()
                    response_data = {"success": True}
                    status_code = 200

            elif path == "/api/toggle_ready":
                player_id = data.get("player_id")
                if GAME_STATE["status"] != "waiting":
                    response_data = {"error": "Tidak bisa mengubah status siap saat ini"}
                else:
                    player = next((p for p in GAME_STATE["players"] if p["id"] == player_id), None)
                    if not player:
                        response_data = {"error": "Pemain tidak ditemukan"}
                    else:
                        player["ready"] = not player["ready"]
                        GAME_STATE["logs"].append(f"{'✅' if player['ready'] else '⏸️'} {player['name']} {'siap bermain' if player['ready'] else 'membatalkan status siap'}.")
                        response_data = {"success": True, "ready": player["ready"]}
                        status_code = 200
                    
            elif path == "/api/draw_stock":
                player_id = data.get("player_id")
                current_player = GAME_STATE["players"][GAME_STATE["current_turn_idx"]]
                if current_player["id"] != player_id: response_data = {"error": "Bukan giliran Anda"}
                elif current_player.get("has_drawn", False):
                    response_data = {"error": "Anda sudah mengambil kartu di giliran ini. Silakan buang kartu untuk lanjut."}
                elif len(GAME_STATE["deck"]) == 0:
                    # Stock habis, game selesai otomatis
                    GAME_STATE["logs"].append("🔄 Stock Pile habis! Game dihentikan.")
                    end_the_game(winner_id=None)
                    response_data = {"success": True}
                    status_code = 200
                else:
                    card = GAME_STATE["deck"].pop()
                    current_player["cards"].append(card)
                    current_player["has_drawn"] = True
                    GAME_STATE["logs"].append(f"🎣 {current_player['name']} mengambil 1 kartu dari Stock Pile.")
                    response_data = {"success": True, "drawn": card}
                    status_code = 200
                    
            elif path == "/api/meld_from_discard":
                player_id = data.get("player_id")
                card_index = data.get("index")
                hand_cards = data.get("hand_cards", [])
                current_player = GAME_STATE["players"][GAME_STATE["current_turn_idx"]]
                pile = GAME_STATE["discard_pile"]

                if current_player["id"] != player_id:
                    response_data = {"error": "Bukan giliran Anda"}
                elif current_player.get("has_drawn", False):
                    response_data = {"error": "Anda sudah mengambil kartu di giliran ini. Silakan buang kartu untuk lanjut."}
                elif card_index is None or card_index < 0 or card_index >= len(pile):
                    response_data = {"error": "Kartu tidak valid"}
                elif (len(pile) - card_index) > 3:
                    response_data = {"error": "Aturan Remi: hanya 3 kartu teratas Discard Pile yang boleh diambil"}
                elif not hand_cards or not all(c in current_player["cards"] for c in hand_cards):
                    response_data = {"error": "Kartu tangan yang dipilih tidak valid"}
                else:
                    target_card = pile[card_index]
                    combo = hand_cards + [target_card]
                    is_seq = check_is_sequence(combo)
                    is_set = check_is_set(combo)

                    if not is_seq and not is_set:
                        response_data = {"error": "Kombinasi Tidak Valid! Kartu discard + kartu tangan harus membentuk Sequence atau Set yang sah."}
                    elif is_set and not current_player["has_sequence"]:
                        response_data = {"error": "Aturan Remi Indo: Anda WAJIB menggelar minimal 1 Sequence sebelum bisa menggelar Set!"}
                    else:
                        extra_cards_on_top = pile[card_index + 1:]
                        remaining_after = (len(current_player["cards"]) - len(hand_cards)) + len(extra_cards_on_top)
                        if remaining_after == 0:
                            response_data = {"error": "Gagal! Anda harus tetap punya kartu tersisa untuk dibuang setelah mengambil kombinasi ini."}
                        else:
                            GAME_STATE["discard_pile"] = pile[:card_index]
                            for c in hand_cards:
                                current_player["cards"].remove(c)
                            current_player["cards"].extend(extra_cards_on_top)
                            current_player["melds"].append(combo)
                            if is_seq:
                                current_player["has_sequence"] = True
                            current_player["has_drawn"] = True
                            type_str = "Sequence" if is_seq else "Set"
                            GAME_STATE["logs"].append(f"🫳✨ {current_player['name']} mengambil {target_card} dari Discard Pile dan langsung menggelar {type_str}: {', '.join(combo)}")
                            response_data = {"success": True}
                            status_code = 200
                    
            elif path == "/api/declare_meld":
                player_id = data.get("player_id")
                selected_cards = data.get("cards", [])
                current_player = GAME_STATE["players"][GAME_STATE["current_turn_idx"]]
                
                if current_player["id"] != player_id:
                    response_data = {"error": "Bukan giliran Anda"}
                elif not all(c in current_player["cards"] for c in selected_cards):
                    response_data = {"error": "Kartu tidak ada di tangan"}
                else:
                    is_seq = check_is_sequence(selected_cards)
                    is_set = check_is_set(selected_cards)
                    
                    if not is_seq and not is_set:
                        response_data = {"error": "Kombinasi Tidak Valid! Pastikan Sequence atau Set murni sesuai aturan."}
                    elif is_set and not current_player["has_sequence"]:
                        response_data = {"error": "Aturan Remi Indo: Anda WAJIB menggelar minimal 1 Sequence (Urutan) sebelum bisa menggelar Set!"}
                    else:
                        if is_seq:
                            current_player["has_sequence"] = True
                        
                        # Pindahkan kartu dari tangan ke meld meja
                        for c in selected_cards:
                            current_player["cards"].remove(c)
                        current_player["melds"].append(selected_cards)
                        
                        type_str = "Sequence" if is_seq else "Set"
                        GAME_STATE["logs"].append(f"✨ {current_player['name']} menggelar {type_str}: {', '.join(selected_cards)}")
                        response_data = {"success": True}
                        status_code = 200
                        
            elif path == "/api/discard_and_turn":
                player_id = data.get("player_id")
                card = data.get("card")
                is_closing = data.get("is_closing", False)
                current_player = GAME_STATE["players"][GAME_STATE["current_turn_idx"]]
                
                if current_player["id"] != player_id:
                    response_data = {"error": "Bukan giliran Anda"}
                elif card not in current_player["cards"]:
                    response_data = {"error": "Kartu tidak valid"}
                else:
                    current_player["cards"].remove(card)
                    GAME_STATE["discard_pile"].append(card)
                    current_player["has_drawn"] = False
                    
                    if is_closing:
                        if len(current_player["cards"]) == 0:
                            GAME_STATE["logs"].append(f"🛑 {current_player['name']} menutup permainan dengan kartu {card}!")
                            end_the_game(player_id, closing_card=card)
                        else:
                            # Rollback jika ternyata masih ada kartu tersisa di tangan
                            current_player["cards"].append(card)
                            GAME_STATE["discard_pile"].pop()
                            response_data = {"error": "Gagal Tutup! Kartu di tangan Anda harus habis total setelah menyisakan 1 kartu penutup."}
                            self.send_response(400)
                            self.send_header('Content-Type', 'application/json')
                            self.send_header('Access-Control-Allow-Origin', '*')
                            self.end_headers()
                            self.wfile.write(json.dumps(response_data).encode('utf-8'))
                            return
                    else:
                        GAME_STATE["logs"].append(f"📤 {current_player['name']} membuang {card}.")
                        advance_turn()
                        
                    response_data = {"success": True}
                    status_code = 200
                    
            elif path == "/api/greedy_suggest":
                player_id = data.get("player_id")
                player = next((p for p in GAME_STATE["players"] if p["id"] == player_id), None)
                if not player:
                    response_data = {"error": "Pemain tidak ditemukan"}
                elif not player["cards"]:
                    response_data = {"error": "Tidak ada kartu di tangan untuk dianalisis"}
                else:
                    groups, discard = greedy_suggest(player["cards"])
                    ready_groups = [g for g in groups if g["ready"]]
                    reasoning = []
                    if ready_groups:
                        for g in ready_groups:
                            label = "Sequence" if g["type"] == "sequence" else "Set"
                            reasoning.append(f"✅ Greedy menemukan {label} siap gelar: {', '.join(g['cards'])}")
                    else:
                        reasoning.append("🔍 Belum ada kombinasi yang benar-benar siap digelar saat ini.")
                    if discard:
                        reasoning.append(f"🗑️ Kartu {discard} paling lemah keterhubungannya dgn kartu lain, disarankan dibuang.")
                    else:
                        reasoning.append("👍 Semua kartu Anda sudah masuk kombinasi potensial!")
                    response_data = {
                        "success": True,
                        "groups": groups,
                        "discard_suggestion": discard,
                        "reasoning": reasoning
                    }
                    status_code = 200

            elif path == "/api/reset":
                player_id = data.get("player_id")
                if player_id != GAME_STATE["host_id"]:
                    response_data = {"error": "Hanya host yang bisa mereset permainan"}
                else:
                    GAME_STATE = {
                        "status": "waiting", "players": [], "deck": [], "discard_pile": [],
                        "current_turn_idx": 0, "logs": ["♻️ Game di-reset oleh host."], "winner_id": None,
                        "host_id": None
                    }
                    response_data = {"success": True}
                    status_code = 200
                
            elif path == "/api/leave":
                player_id = data.get("player_id")
                success = handle_player_leave(player_id)
                response_data = {"success": success}
                status_code = 200

        self.send_response(status_code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(json.dumps(response_data).encode('utf-8'))

class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True

def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('10.255.255.255', 1))
        IP = s.getsockname()[0]
    except:
        IP = '127.0.0.1'
    finally:
        s.close()
    return IP

# ==========================================
# MODERN WEB INTERFACE (FRONTEND HTML/JS)
# ==========================================
HTML_CONTENT = """<!DOCTYPE html>
<html lang="id">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Indonesian Rummy (Remi) Jaringan Lokal</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        body { background-color: #0b1329; color: #f1f5f9; }
        .felt-table {
            background: radial-gradient(circle, #0f5132 0%, #062416 100%);
            box-shadow: inset 0 0 80px rgba(0,0,0,0.9);
        }
        .selected-card { transform: translateY(-15px) !important; border-color: #fbbf24 !important; box-shadow: 0 0 15px #fbbf24; }
        .hand-card { touch-action: none; }
        .drag-ghost { opacity: 0.9; pointer-events: none; position: fixed; z-index: 999; }
    </style>
</head>
<body class="min-h-screen flex flex-col font-sans select-none">

    <!-- MODAL JOIN -->
    <div id="join-modal" class="fixed inset-0 bg-slate-950/95 flex items-center justify-center z-50">
        <div class="bg-slate-900 border border-slate-800 p-8 rounded-3xl text-center max-w-md w-full mx-4 shadow-2xl">
            <h1 class="text-2xl font-black mb-2 text-yellow-400 tracking-wider">🃏 REMI INDONESIA 🇮🇩</h1>
            <p class="text-slate-400 text-xs mb-6">Game Kartu Rummy Aturan Resmi Greedy</p>
            <div id="join-form-container">
                <input type="text" id="join-name" maxlength="12" class="w-full px-4 py-3 bg-slate-800 border border-slate-700 text-white font-bold rounded-xl text-center text-lg mb-4 focus:ring-2 focus:ring-yellow-500 outline-none" placeholder="Nama Anda...">
                <button onclick="joinGame()" class="w-full bg-yellow-500 hover:bg-yellow-600 text-slate-950 font-black py-4 rounded-xl text-lg transition transform active:scale-95">MASUK LOBBY</button>
            </div>
            <div id="join-wait-container" class="hidden py-4 text-slate-400 text-sm animate-pulse">⏳ Game Sedang Berjalan. Harap tunggu game selesai...</div>
            <p class="text-center text-[10px] text-slate-600 mt-6">developer by dhamas and teams</p>
        </div>
    </div>

    <!-- LOBBY SCREEN -->
    <div id="lobby-screen" class="hidden max-w-md mx-auto mt-20 bg-slate-900 border border-slate-800 p-6 rounded-3xl shadow-xl w-full">
        <h2 class="text-2xl font-bold text-yellow-400 mb-4 text-center">Ruang Tunggu Remi</h2>
        <div id="server-info-box" class="bg-slate-950 border border-slate-800/70 rounded-xl px-4 py-2 mb-4 text-center">
            <p class="text-[10px] text-slate-500 uppercase tracking-wide mb-0.5">Device lain sambungkan ke</p>
            <p id="server-info-text" class="text-sm font-mono font-bold text-emerald-400">memuat...</p>
        </div>
        <div class="bg-slate-950 rounded-2xl p-4 mb-6">
            <ul id="lobby-players-list" class="space-y-2"></ul>
        </div>
        <button id="start-game-btn" onclick="startGame()" class="hidden w-full bg-emerald-500 hover:bg-emerald-600 text-slate-950 font-black py-4 rounded-xl transition text-lg active:scale-95">MULAI PERMAINAN (2-4 Pemain)</button>
        <button id="ready-toggle-btn" onclick="toggleReady()" class="hidden w-full font-black py-4 rounded-xl transition text-lg active:scale-95">SIAP</button>
        <p id="waiting-host-msg" class="hidden text-center text-xs text-slate-400 font-bold mt-3 animate-pulse">⏳ Menunggu host memulai permainan...</p>
        <p class="text-center text-[10px] text-slate-600 mt-4">developer by dhamas and teams</p>
    </div>

    <!-- MAIN GAME SCREEN -->
    <div id="game-screen" class="hidden max-w-7xl mx-auto w-full px-4 py-4 flex-1 flex flex-col lg:grid lg:grid-cols-4 gap-6">
        
        <!-- SIDEBAR INFO -->
        <div class="lg:col-span-1 bg-slate-900 border border-slate-800 rounded-3xl p-4 flex flex-col justify-between max-h-[600px] lg:max-h-none">
            <div>
                <h3 class="font-bold text-slate-300 border-b border-slate-800 pb-2 mb-3">👥 Daftar Pemain</h3>
                <div id="players-game-list" class="space-y-2"></div>
            </div>
            <div class="mt-4 flex-1 flex flex-col justify-end">
                <h4 class="text-xs font-bold text-slate-500 uppercase mb-2">Aktivitas Permainan</h4>
                <div id="game-logs" class="bg-slate-950 rounded-2xl p-3 border border-slate-800/60 text-[11px] font-mono space-y-1 h-36 overflow-y-auto"></div>
            </div>
            <div class="mt-4 space-y-2">
                <audio id="bgm-audio" src="/bgm3.mp3" loop preload="auto"></audio>
                <button id="mute-btn" onclick="toggleMute()" class="w-full bg-slate-800 border border-slate-700 text-slate-300 font-bold py-2 rounded-xl text-xs">🔊 Suara: Nyala</button>
                <button id="ingame-reset-btn" onclick="resetGame()" class="hidden w-full bg-red-950/40 border border-red-900/30 text-red-400 font-bold py-2 rounded-xl text-xs">♻️ Reset Game</button>
                <p class="text-center text-[10px] text-slate-600 pt-2">developer by dhamas and teams</p>
            </div>
        </div>

        <!-- PLAYING AREA -->
        <div class="lg:col-span-3 flex flex-col gap-4">
            
            <!-- STATUS TOPBAR -->
            <div id="status-bar" class="bg-slate-900 border border-slate-800 rounded-2xl p-4 flex justify-between items-center">
                <div>
                    <h2 id="status-message" class="font-black text-lg text-yellow-400">Loading...</h2>
                    <p id="status-submessage" class="text-xs text-slate-400">Aturan: Wajib punya 1 Sequence sebelum menggelar Set!</p>
                </div>
                <div id="action-meld-container" class="hidden gap-2">
                    <button onclick="declareMeld()" class="bg-blue-600 hover:bg-blue-700 text-white px-4 py-2 rounded-xl text-xs font-black shadow-lg">✨ Gelar Kombinasi</button>
                </div>
            </div>

            <!-- FELT GAME TABLE -->
            <div class="felt-table rounded-[2rem] border-4 border-slate-950 p-6 flex flex-col justify-between min-h-[350px] relative">
                
                <!-- TOP: MEJA MELDS / AREA KOMBINASI TERGELAR -->
                <div class="w-full mb-6">
                    <span class="text-[10px] uppercase font-black tracking-wider text-emerald-400 block mb-2">Kombinasi Tergelar di Meja:</span>
                    <div id="table-melds-container" class="grid grid-cols-2 md:grid-cols-3 gap-4"></div>
                </div>

                <!-- BOTTOM: DECK & DISCARD PILE -->
                <div class="flex justify-center items-center gap-12 mt-auto">
                    <!-- STOCK PILE -->
                    <div class="text-center">
                        <div onclick="drawStock()" class="bg-indigo-950 border-2 border-indigo-600 rounded-2xl w-20 h-28 flex flex-col items-center justify-center shadow-xl cursor-pointer active:scale-95 group">
                            <span class="text-[9px] font-black text-indigo-400">STOCK PILE</span>
                            <span id="deck-count" class="text-3xl font-black text-white">0</span>
                        </div>
                    </div>

                    <!-- DISCARD PILE (3 kartu teratas bisa diambil, sisanya hanya bisa dilihat) -->
                    <div class="text-center">
                        <span class="text-[9px] font-black text-slate-400 block mb-1">DISCARD PILE (3 teratas bisa diambil)</span>
                        <div id="discard-pile-container" class="flex flex-wrap items-center gap-1.5 overflow-x-auto p-2 bg-black/20 rounded-2xl min-w-[120px] max-w-md justify-center"></div>
                    </div>
                </div>
            </div>

            <!-- USER HAND CONTROLLER -->
            <div class="bg-slate-900 border border-slate-800 rounded-3xl p-6">
                <div class="flex justify-between items-center mb-2 flex-wrap gap-2">
                    <h3 class="text-xs font-bold uppercase tracking-wider text-slate-400">🃏 Kartu Tangan Anda (Tap = pilih • Tahan &amp; geser = susun bebas urutan)</h3>
                    <div class="flex gap-2 flex-wrap">
                        <button onclick="greedySuggest()" class="bg-indigo-600 hover:bg-indigo-700 text-white font-black px-3 py-1.5 rounded-xl text-xs transition">🧠 Saran Greedy</button>
                        <button onclick="discardCard(false)" class="bg-orange-600 hover:bg-orange-700 text-white font-black px-3 py-1.5 rounded-xl text-xs transition">📤 Buang Kartu</button>
                        <button onclick="discardCard(true)" class="bg-red-600 hover:bg-red-700 text-white font-black px-3 py-1.5 rounded-xl text-xs transition">🛑 Tutup Game (Remi)</button>
                    </div>
                </div>
                <div id="greedy-hint-box" class="hidden mb-3 bg-indigo-950/40 border border-indigo-800/50 rounded-xl p-2 text-[11px] text-indigo-200 space-y-0.5"></div>
                <div id="your-hand-container" class="flex flex-wrap justify-center gap-3 min-h-[120px] py-2"></div>
            </div>

        </div>
    </div>

    <!-- MODAL GAME OVER -->
    <div id="game-over-modal" class="hidden fixed inset-0 bg-slate-950/95 flex items-center justify-center z-50">
        <div class="bg-slate-900 border border-slate-800 p-8 rounded-3xl text-center max-w-md w-full mx-4 shadow-2xl">
            <h2 class="text-4xl font-black text-red-500 mb-2">🏁 SELESAI!</h2>
            <div class="bg-slate-950 rounded-2xl p-4 my-4 text-left border border-slate-800">
                <h3 class="text-xs font-bold text-slate-500 uppercase mb-3">Papan Nilai Akhir:</h3>
                <div id="game-results-list" class="space-y-2"></div>
            </div>
            <button id="gameover-reset-btn" onclick="resetGame()" class="hidden w-full bg-yellow-500 hover:bg-yellow-600 text-slate-950 font-black py-4 rounded-xl text-lg">MAIN LAGI</button>
            <p id="gameover-waiting-host-msg" class="hidden text-center text-xs text-slate-400 font-bold mt-3 animate-pulse">⏳ Menunggu host mereset lobby...</p>
        </div>
    </div>

    <script>
        let playerId = localStorage.getItem("remi_player_id") || null;
        let playerName = localStorage.getItem("remi_player_name") || null;
        let selectedCards = [];
        let pollInterval = null;
        let lastKnownStatus = null;

        // ================= AUDIO ENGINE =================
        // BGM (musik latar) diputar dari file asli bgm3.mp3 lewat elemen <audio>.
        // SFX kartu & tombol (draw, discard, meld, invalid, join, win) tetap disintesis lewat Web Audio API.
        const AudioEngine = (function() {
            let ctx = null, masterGain = null, sfxGain = null;
            let muted = false;
            const bgmAudio = document.getElementById("bgm-audio");
            bgmAudio.volume = 0.35;

            // Skala pentatonik ala Slendro/Pelog (disederhanakan agar musikal di Web Audio) — dipakai untuk SFX
            const SCALE = [220.00, 246.94, 277.18, 329.63, 369.99, 440.00, 493.88, 554.37, 659.25];

            function ensureCtx() {
                if (!ctx) {
                    ctx = new (window.AudioContext || window.webkitAudioContext)();
                    masterGain = ctx.createGain(); masterGain.gain.value = 0.5; masterGain.connect(ctx.destination);
                    sfxGain = ctx.createGain(); sfxGain.gain.value = 0.8; sfxGain.connect(masterGain);
                }
                if (ctx.state === "suspended") ctx.resume();
                return ctx;
            }

            function startMusic() {
                bgmAudio.muted = muted;
                bgmAudio.play().catch(() => {}); // menunggu gesture user jika autoplay diblokir browser
            }

            function stopMusic() {
                bgmAudio.pause();
                bgmAudio.currentTime = 0;
            }

            // Nada 'metalik' ala gamelan: sine + overtone sedikit detune yang cepat meredup
            function pluckTone(freq, time, dur, dest, vol) {
                const osc = ctx.createOscillator(), osc2 = ctx.createOscillator();
                const gain = ctx.createGain(), g2 = ctx.createGain();
                osc.type = "sine"; osc2.type = "sine";
                osc.frequency.setValueAtTime(freq, time);
                osc2.frequency.setValueAtTime(freq * 2.01, time);
                g2.gain.setValueAtTime(0.25, time);
                osc2.connect(g2); g2.connect(gain); osc.connect(gain);
                gain.gain.setValueAtTime(0.0001, time);
                gain.gain.linearRampToValueAtTime(vol, time + 0.01);
                gain.gain.exponentialRampToValueAtTime(0.0001, time + dur);
                gain.connect(dest);
                osc.start(time); osc2.start(time);
                osc.stop(time + dur + 0.05); osc2.stop(time + dur + 0.05);
            }

            // Hentakan 'gong' bernada rendah untuk penanda irama / kemenangan
            function gongHit(time, dest, vol) {
                const osc = ctx.createOscillator();
                osc.type = "sine"; osc.frequency.setValueAtTime(80, time);
                const gain = ctx.createGain();
                gain.gain.setValueAtTime(0.0001, time);
                gain.gain.linearRampToValueAtTime(vol, time + 0.02);
                gain.gain.exponentialRampToValueAtTime(0.0001, time + 1.8);
                osc.connect(gain); gain.connect(dest);
                osc.start(time); osc.stop(time + 1.9);
            }

            function playDraw() {
                ensureCtx();
                const t = ctx.currentTime;
                const bufferSize = Math.floor(ctx.sampleRate * 0.12);
                const buffer = ctx.createBuffer(1, bufferSize, ctx.sampleRate);
                const data = buffer.getChannelData(0);
                for (let i = 0; i < bufferSize; i++) data[i] = (Math.random() * 2 - 1) * (1 - i / bufferSize);
                const noise = ctx.createBufferSource(); noise.buffer = buffer;
                const filter = ctx.createBiquadFilter(); filter.type = "highpass"; filter.frequency.value = 2500;
                const gain = ctx.createGain();
                gain.gain.setValueAtTime(0.6, t);
                gain.gain.exponentialRampToValueAtTime(0.001, t + 0.12);
                noise.connect(filter); filter.connect(gain); gain.connect(sfxGain);
                noise.start(t);
            }

            function playDiscard() {
                ensureCtx();
                pluckTone(220, ctx.currentTime, 0.15, sfxGain, 0.6);
            }

            function playMeld() {
                ensureCtx();
                const t = ctx.currentTime;
                [4, 5, 7].forEach((idx, i) => pluckTone(SCALE[idx], t + i * 0.09, 0.4, sfxGain, 0.7));
            }

            function playInvalid() {
                ensureCtx();
                const t = ctx.currentTime;
                const osc = ctx.createOscillator();
                osc.type = "square";
                osc.frequency.setValueAtTime(160, t);
                osc.frequency.exponentialRampToValueAtTime(90, t + 0.2);
                const gain = ctx.createGain();
                gain.gain.setValueAtTime(0.25, t);
                gain.gain.exponentialRampToValueAtTime(0.001, t + 0.25);
                osc.connect(gain); gain.connect(sfxGain);
                osc.start(t); osc.stop(t + 0.3);
            }

            function playJoin() {
                ensureCtx();
                const t = ctx.currentTime;
                pluckTone(SCALE[2], t, 0.5, sfxGain, 0.6);
                pluckTone(SCALE[5], t + 0.1, 0.6, sfxGain, 0.5);
            }

            function playWin() {
                ensureCtx();
                const t = ctx.currentTime;
                gongHit(t, sfxGain, 0.9);
                [0, 2, 4, 5, 7].forEach((idx, i) => pluckTone(SCALE[idx], t + 0.15 + i * 0.14, 0.8, sfxGain, 0.6));
            }

            function toggleMuted() {
                muted = !muted;
                if (masterGain) masterGain.gain.value = muted ? 0 : 0.5;
                bgmAudio.muted = muted;
                return muted;
            }

            return { ensureCtx, startMusic, stopMusic, playDraw, playDiscard, playMeld, playInvalid, playJoin, playWin, toggleMuted, isMuted: () => muted };
        })();

        async function apiPost(endpoint, body = {}) {
            const res = await fetch(endpoint, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
            return await res.json();
        }

        async function apiGet(endpoint) {
            const res = await fetch(endpoint);
            return await res.json();
        }

        async function loadServerInfo() {
            try {
                const info = await apiGet("/api/server_info");
                const el = document.getElementById("server-info-text");
                if (el) el.textContent = `http://${info.ip}:${info.port}`;
            } catch (e) {
                const el = document.getElementById("server-info-text");
                if (el) el.textContent = "Gagal memuat alamat server";
            }
        }

        async function joinGame() {
            const name = document.getElementById("join-name").value.trim();
            if(!name) return alert("Isi nama dulu!");
            AudioEngine.ensureCtx();
            const res = await apiPost("/api/join", { name });
            if(res.success) {
                playerId = res.player_id; playerName = res.player_name;
                localStorage.setItem("remi_player_id", playerId);
                localStorage.setItem("remi_player_name", playerName);
                document.getElementById("join-modal").classList.add("hidden");
                AudioEngine.playJoin();
                AudioEngine.startMusic();
                startPolling();
            } else { AudioEngine.playInvalid(); alert(res.error); }
        }

        async function startGame() {
            const res = await apiPost("/api/start", { player_id: playerId });
            if(!res.success) alert(res.error);
        }

        async function toggleReady() {
            const res = await apiPost("/api/toggle_ready", { player_id: playerId });
            if(!res.success) alert(res.error);
            else pollState();
        }

        async function drawStock() {
            const res = await apiPost("/api/draw_stock", { player_id: playerId });
            if(res.success) AudioEngine.playDraw();
            else { AudioEngine.playInvalid(); alert(res.error); }
        }

        async function meldFromDiscard(idx) {
            if(selectedCards.length === 0) {
                alert("Pilih dulu kartu di tangan yang mau digabungkan, baru klik kartu Discard Pile ini.");
                return;
            }
            if(confirm(`Gabungkan ${selectedCards.length} kartu tangan terpilih dengan kartu ini menjadi kombinasi?`)) {
                const res = await apiPost("/api/meld_from_discard", { player_id: playerId, index: idx, hand_cards: selectedCards });
                if(res.success) { selectedCards = []; clearGreedyHints(); AudioEngine.playDraw(); AudioEngine.playMeld(); }
                else { AudioEngine.playInvalid(); alert(res.error); }
            }
        }

        function toggleSelectCard(card) {
            const idx = selectedCards.indexOf(card);
            if(idx > -1) selectedCards.splice(idx, 1);
            else selectedCards.push(card);
            renderHandOnly();
        }

        async function declareMeld() {
            if(selectedCards.length < 3) return alert("Pilih minimal 3 kartu untuk membentuk kombinasi!");
            const res = await apiPost("/api/declare_meld", { player_id: playerId, cards: selectedCards });
            if(res.success) { selectedCards = []; clearGreedyHints(); AudioEngine.playMeld(); }
            else { AudioEngine.playInvalid(); alert(res.error); }
        }

        async function discardCard(isClosing = false) {
            if(selectedCards.length !== 1) return alert("Pilih tepat 1 kartu dari tangan untuk dibuang/ditutup!");
            const res = await apiPost("/api/discard_and_turn", { player_id: playerId, card: selectedCards[0], is_closing: isClosing });
            if(res.success) { selectedCards = []; clearGreedyHints(); if(isClosing) AudioEngine.playWin(); else AudioEngine.playDiscard(); }
            else { AudioEngine.playInvalid(); alert(res.error); }
        }

        async function resetGame() {
            if(confirm("Reset game?")) {
                const res = await apiPost("/api/reset", { player_id: playerId });
                if(res.success) { location.reload(); }
                else if(res.error) { alert(res.error); }
            }
        }

        function toggleMute() {
            const nowMuted = AudioEngine.toggleMuted();
            const btn = document.getElementById("mute-btn");
            btn.textContent = nowMuted ? "🔇 Suara: Mati" : "🔊 Suara: Nyala";
        }

        function getCardHTML(cardStr, clickHandler='', extraClass='', isHandCard=false) {
            const [suit, val] = cardStr.split("-");
            const suitMap = { "S": "♠", "H": "♥", "D": "♦", "C": "♣" };
            const isRed = (suit === "H" || suit === "D");
            const color = isRed ? "text-red-500 border-red-900/40 bg-slate-950" : "text-slate-200 border-slate-800 bg-slate-950";
            const onclickAttr = isHandCard ? '' : `onclick="${clickHandler}"`;
            const dragAttr = isHandCard ? `data-card="${cardStr}" onpointerdown="handleCardPointerDown(event, '${cardStr}')"` : '';
            const handClass = isHandCard ? 'hand-card' : '';
            return `
                <div ${onclickAttr} ${dragAttr} class="border-2 rounded-xl p-2 flex flex-col justify-between w-14 h-20 text-xs shadow-md transform transition duration-150 cursor-pointer ${handClass} ${color} ${extraClass}">
                    <div class="flex justify-between font-black"><span>${val}</span><span>${suitMap[suit]}</span></div>
                    <div class="text-xl text-center font-bold">${suitMap[suit]}</div>
                </div>
            `;
        }

        // ================= SUSUN BEBAS KARTU DI TANGAN (drag & reorder) =================
        function handOrderKey() { return `remi_hand_order_${playerId}`; }

        function syncHandOrder(serverCards) {
            let stored = [];
            try { stored = JSON.parse(localStorage.getItem(handOrderKey())) || []; } catch(e) { stored = []; }
            let ordered = stored.filter(c => serverCards.includes(c));
            serverCards.forEach(c => { if(!ordered.includes(c)) ordered.push(c); });
            window.currentHandOrder = ordered;
            localStorage.setItem(handOrderKey(), JSON.stringify(ordered));
        }

        function persistHandOrder() {
            if(playerId) localStorage.setItem(handOrderKey(), JSON.stringify(window.currentHandOrder));
        }

        function reorderHand(cardMoved, targetCard) {
            if(cardMoved === targetCard) return;
            const order = window.currentHandOrder;
            const idxA = order.indexOf(cardMoved);
            const idxB = order.indexOf(targetCard);
            if(idxA === -1 || idxB === -1) return;
            order.splice(idxA, 1);
            order.splice(idxB, 0, cardMoved);
            renderHandOnly();
        }

        let dragState = null;
        const DRAG_THRESHOLD = 10;

        function handleCardPointerDown(e, card) {
            const el = e.currentTarget;
            dragState = { card, startX: e.clientX, startY: e.clientY, moved: false, sourceEl: el, ghost: null };
            document.addEventListener('pointermove', onHandDragMove);
            document.addEventListener('pointerup', onHandDragEnd);
        }

        function onHandDragMove(e) {
            if(!dragState) return;
            const dx = e.clientX - dragState.startX, dy = e.clientY - dragState.startY;
            if(!dragState.moved && Math.hypot(dx, dy) > DRAG_THRESHOLD) {
                dragState.moved = true;
                const rect = dragState.sourceEl.getBoundingClientRect();
                dragState.offsetX = dragState.startX - rect.left;
                dragState.offsetY = dragState.startY - rect.top;
                dragState.ghost = dragState.sourceEl.cloneNode(true);
                dragState.ghost.classList.add('drag-ghost');
                dragState.ghost.style.width = rect.width + 'px';
                dragState.ghost.style.left = rect.left + 'px';
                dragState.ghost.style.top = rect.top + 'px';
                document.body.appendChild(dragState.ghost);
                dragState.sourceEl.style.opacity = '0.2';
                AudioEngine.playDraw();
            }
            if(dragState.moved) {
                dragState.ghost.style.left = (e.clientX - dragState.offsetX) + 'px';
                dragState.ghost.style.top = (e.clientY - dragState.offsetY) + 'px';
                const container = document.getElementById('your-hand-container');
                let closest = null, closestDist = Infinity;
                for(const c of container.children) {
                    if(c === dragState.sourceEl) continue;
                    const r = c.getBoundingClientRect();
                    const dist = Math.hypot(e.clientX - (r.left + r.width/2), e.clientY - (r.top + r.height/2));
                    if(dist < closestDist) { closestDist = dist; closest = c; }
                }
                if(closest && closestDist < 70) {
                    reorderHand(dragState.card, closest.dataset.card);
                }
            }
        }

        function onHandDragEnd(e) {
            if(!dragState) return;
            if(dragState.moved) {
                dragState.ghost.remove();
                dragState.sourceEl.style.opacity = '';
                persistHandOrder();
            } else {
                toggleSelectCard(dragState.card);
            }
            dragState = null;
            document.removeEventListener('pointermove', onHandDragMove);
            document.removeEventListener('pointerup', onHandDragEnd);
        }

        // ================= SARAN GREEDY (mesin analisis kombinasi otomatis) =================
        async function greedySuggest() {
            const res = await apiPost("/api/greedy_suggest", { player_id: playerId });
            if(!res.success) { AudioEngine.playInvalid(); alert(res.error); return; }
            window.greedyGroupColor = {};
            const palette = ["ring-2 ring-emerald-400", "ring-2 ring-sky-400", "ring-2 ring-fuchsia-400", "ring-2 ring-orange-400"];
            let ci = 0;
            res.groups.forEach(g => {
                if(g.type === "floating") return;
                const cls = palette[ci % palette.length]; ci++;
                g.cards.forEach(c => { window.greedyGroupColor[c] = cls; });
            });
            window.greedySuggestDiscard = res.discard_suggestion;
            renderHandOnly();
            const box = document.getElementById("greedy-hint-box");
            box.innerHTML = res.reasoning.map(r => `<div>${r}</div>`).join("");
            box.classList.remove("hidden");
            AudioEngine.playMeld();
        }

        function clearGreedyHints() {
            window.greedyGroupColor = {};
            window.greedySuggestDiscard = null;
            const box = document.getElementById("greedy-hint-box");
            if(box) box.classList.add("hidden");
        }

        function renderHandOnly() {
            if(!window.currentHandOrder) return;
            const handContainer = document.getElementById("your-hand-container");
            handContainer.innerHTML = "";
            window.currentHandOrder.forEach(card => {
                const isSelected = selectedCards.includes(card);
                let extra = isSelected ? "selected-card" : "hover:-translate-y-2";
                if(window.greedySuggestDiscard === card) extra += " ring-2 ring-red-500";
                else if(window.greedyGroupColor && window.greedyGroupColor[card]) extra += " " + window.greedyGroupColor[card];
                handContainer.innerHTML += getCardHTML(card, '', extra, true);
            });
        }

        function renderState(state) {
            if (!state) return;

            if(state.status === "game_over" && lastKnownStatus !== "game_over") {
                AudioEngine.playWin();
            }
            lastKnownStatus = state.status;
            
            // Cek status keikutsertaan pemain
            if(playerId && !state.players.some(p => p.id === playerId)) {
                localStorage.clear(); playerId = null; location.reload(); return;
            }

            if(playerId === null) {
                document.getElementById("join-modal").classList.remove("hidden");
                document.getElementById("join-wait-container").className = state.status === "playing" ? "block animate-pulse text-xs mt-2" : "hidden";
                return;
            }

            if(state.status === "waiting") {
                document.getElementById("lobby-screen").classList.remove("hidden");
                document.getElementById("game-screen").classList.add("hidden");
                const isHost = state.host_id === playerId;
                const list = document.getElementById("lobby-players-list");
                list.innerHTML = state.players.map(p => {
                    const badge = p.is_host
                        ? '<span class="text-yellow-400">👑 Host</span>'
                        : (p.ready ? '<span class="text-emerald-400">✅ Siap</span>' : '<span class="text-slate-500">⏸️ Belum siap</span>');
                    return `<li class="p-2 bg-slate-800 border border-slate-700 rounded-xl flex justify-between font-bold"><span>👤 ${p.name}</span>${badge}</li>`;
                }).join("");

                const startBtn = document.getElementById("start-game-btn");
                const readyBtn = document.getElementById("ready-toggle-btn");
                const waitingMsg = document.getElementById("waiting-host-msg");

                if (isHost) {
                    startBtn.classList.remove("hidden");
                    readyBtn.classList.add("hidden");
                    waitingMsg.classList.add("hidden");
                } else {
                    startBtn.classList.add("hidden");
                    readyBtn.classList.remove("hidden");
                    waitingMsg.classList.remove("hidden");
                    const me = state.players.find(p => p.id === playerId);
                    const iAmReady = me ? me.ready : false;
                    readyBtn.textContent = iAmReady ? "BATAL" : "SIAP";
                    readyBtn.className = iAmReady
                        ? "w-full bg-slate-700 hover:bg-slate-600 text-slate-200 font-black py-4 rounded-xl transition text-lg active:scale-95"
                        : "w-full bg-emerald-500 hover:bg-emerald-600 text-slate-950 font-black py-4 rounded-xl transition text-lg active:scale-95";
                }
                return;
            }

            // MODE PLAYING / GAME OVER
            document.getElementById("lobby-screen").classList.add("hidden");
            document.getElementById("game-screen").classList.remove("hidden");
            
            const ingameResetBtn = document.getElementById("ingame-reset-btn");
            if (state.host_id === playerId) ingameResetBtn.classList.remove("hidden");
            else ingameResetBtn.classList.add("hidden");
            
            // Simpan kartu untuk render seleksi lokal, sambil menjaga urutan bebas yang diatur pemain
            syncHandOrder(state.your_cards);
            renderHandOnly();

            // Render List Pemain Samping
            const isMyTurn = state.current_turn === playerId;
            document.getElementById("players-game-list").innerHTML = state.players.map(p => {
                const isTurn = state.current_turn === p.id;
                return `
                    <div class="p-2 border rounded-xl flex flex-col gap-1 ${isTurn ? 'bg-yellow-500/10 border-yellow-500/40' : 'bg-slate-950/40 border-slate-800'}">
                        <div class="flex justify-between font-bold text-xs ${isTurn ? 'text-yellow-400':''}">
                            <span>${isTurn ? '👉':''} ${p.name}</span>
                            <span class="bg-slate-800 text-[10px] px-2 py-0.5 rounded">${p.card_count} Kartu</span>
                        </div>
                        <div class="flex justify-between text-[10px] text-slate-400">
                            <span>Seq Status: ${p.has_sequence ? '✅ Beres':'❌ Belum'}</span>
                            <span class="text-yellow-500 font-bold">${p.score} Pts</span>
                        </div>
                    </div>
                `;
            }).join("");

            // Render Kombinasi Tergelar di Meja
            const tableMelds = document.getElementById("table-melds-container");
            tableMelds.innerHTML = "";
            state.players.forEach(p => {
                p.melds.forEach(meld => {
                    tableMelds.innerHTML += `
                        <div class="bg-slate-950/60 border border-emerald-900/40 p-2 rounded-xl flex flex-col gap-1">
                            <span class="text-[9px] text-emerald-400 font-black truncate">${p.name}:</span>
                            <div class="flex flex-wrap gap-1">${meld.map(c => getCardHTML(c, '', 'pointer-events-none w-10 h-14 text-[9px]')).join("")}</div>
                        </div>
                    `;
                });
            });

            // Render Discard Pile (hanya 3 kartu teratas ditampilkan & bisa diambil, sisanya disembunyikan)
            const discardCont = document.getElementById("discard-pile-container");
            if(state.discard_pile.length === 0) {
                discardCont.innerHTML = `<div class="text-slate-500 text-xs p-4">Kosong</div>`;
            } else {
                const total = state.discard_pile.length;
                const visibleCount = Math.min(3, total);
                const hiddenCount = total - visibleCount;
                const visibleCards = state.discard_pile.slice(total - visibleCount);
                let html = '';
                if (hiddenCount > 0) {
                    html += `<div class="flex items-center justify-center w-10 h-20 text-[10px] text-slate-500 font-bold">+${hiddenCount}</div>`;
                }
                html += visibleCards.map((c, i) => {
                    const realIdx = total - visibleCount + i;
                    return getCardHTML(c, `meldFromDiscard(${realIdx})`, 'hover:scale-105 border-yellow-500/70 ring-2 ring-yellow-400/50');
                }).join("");
                discardCont.innerHTML = html;
            }

            // Top Status Bar
            document.getElementById("deck-count").textContent = state.deck_count;
            document.getElementById("action-meld-container").className = isMyTurn ? "flex" : "hidden";
            
            const msg = document.getElementById("status-message");
            if(isMyTurn) { msg.textContent = "🛡️ Giliran Anda!"; msg.className = "font-black text-lg text-yellow-400 animate-pulse"; }
            else { msg.textContent = `Giliran ${state.current_turn_name}...`; msg.className = "font-black text-lg text-slate-300"; }

            // Logs
            const logsBox = document.getElementById("game-logs");
            logsBox.innerHTML = state.logs.map(l => `<div class="border-b border-slate-900 py-0.5 text-slate-300">${l}</div>`).join("");
            logsBox.scrollTop = logsBox.scrollHeight;

            // Game Over Handlers
            if(state.status === "game_over") {
                document.getElementById("game-over-modal").classList.remove("hidden");
                const gameoverResetBtn = document.getElementById("gameover-reset-btn");
                const gameoverWaitingMsg = document.getElementById("gameover-waiting-host-msg");
                if (state.host_id === playerId) {
                    gameoverResetBtn.classList.remove("hidden");
                    gameoverWaitingMsg.classList.add("hidden");
                } else {
                    gameoverResetBtn.classList.add("hidden");
                    gameoverWaitingMsg.classList.remove("hidden");
                }
                document.getElementById("game-results-list").innerHTML = state.players.sort((a,b)=>b.score - a.score).map((p, i) => `
                    <div class="flex justify-between p-2 bg-slate-900 border border-slate-800 rounded-xl font-bold">
                        <span>🏆 Rank ${i+1}: ${p.name}</span>
                        <span class="text-yellow-400">${p.score} Poin</span>
                    </div>
                `).join("");
            }
        }

        async function pollState() {
            const url = playerId ? `/api/status?player_id=${playerId}` : "/api/status";
            const state = await apiGet(url);
            renderState(state);
        }

        function startPolling() {
            if(pollInterval) clearInterval(pollInterval);
            pollState(); pollInterval = setInterval(pollState, 1000);
        }

        startPolling();
        loadServerInfo();
    </script>
</body>
</html>
"""

# ==========================================
# SERVER STARTER ENGINE
# ==========================================
def run_server():
    port = PORT
    local_ip = get_local_ip()
    try:
        httpd = ThreadedHTTPServer(('', port), GameRequestHandler)
        print("=================================================================")
        print("🃏 MULTIPLAYER GAME RUMMY INDONESIA (REMI) ONLINE LOCAL SERVER 🃏")
        print("=================================================================")
        print(f"👉 Komputer Server:  http://localhost:{port}")
        print(f"👉 HP Android/iOS:   http://{local_ip}:{port}")
        print("💡 Pastikan HP & Laptop tersambung ke 1 Router / Wi-Fi yang sama.")
        print("=================================================================")
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server...")
        sys.exit(0)

if __name__ == '__main__':
    run_server()
