import time
from pathlib import Path
from playwright.sync_api import sync_playwright

ARTIFACT_DIR = Path(r"C:\Users\Acer\.gemini\antigravity-ide\brain\44ccb128-e339-4f87-b9bb-c9ed657d504e")
ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1024, "height": 768})
    
    # 1. Load localhost
    print("Navigating to http://127.0.0.1:8765 ...")
    page.goto("http://127.0.0.1:8765", wait_until="networkidle")
    time.sleep(1)
    
    # Take screenshot of initial board
    shot1 = ARTIFACT_DIR / "chess_localhost_initial.png"
    page.screenshot(path=str(shot1), full_page=True)
    print(f"Captured initial board: {shot1}")
    
    # 2. Inspect elements
    squares = page.query_selector_all(".square")
    print(f"Found {len(squares)} board squares rendered on page")
    
    # 3. Click available squares to test interaction
    if squares:
        squares[0].click()
        time.sleep(1)
        shot2 = ARTIFACT_DIR / "chess_localhost_selected.png"
        page.screenshot(path=str(shot2), full_page=True)
        print(f"Captured square selection: {shot2}")
        
    # 4. Click New Game button
    new_game = page.query_selector("#new-game")
    if new_game:
        new_game.click()
        time.sleep(1)
        shot3 = ARTIFACT_DIR / "chess_localhost_new_game.png"
        page.screenshot(path=str(shot3), full_page=True)
        print(f"Captured new game reset: {shot3}")
        
    # Check page status text
    status_text = page.inner_text("#status") if page.query_selector("#status") else "N/A"
    print("Status text:", status_text)
    
    # 5. Also query the backend HTTP API directly to show live responses
    response = page.request.get("http://127.0.0.1:8765/state")
    print("Backend /state JSON:", response.text()[:200])

    browser.close()
    print("Playwright live test completed successfully!")
