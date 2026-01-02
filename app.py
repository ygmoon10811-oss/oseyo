def confirm_addr_by_label(cands, label, detail):
    label = (label or "").strip()
    if not label:
        return (
            "⚠️ 주소 후보를 선택해 달라.",
            "", "", None, None,
            "**선택된 장소:** *(아직 없음)*",  # ✅ 추가
            gr.update(visible=False), gr.update(visible=False), gr.update(visible=False),
            gr.update(visible=True),  gr.update(visible=True),  gr.update(visible=True),
        )

    chosen = None
    for c in (cands or []):
        if c.get("label") == label:
            chosen = c
            break

    if not chosen:
        return (
            "⚠️ 선택한 주소를 다시 선택해 달라.",
            "", "", None, None,
            "**선택된 장소:** *(아직 없음)*",  # ✅ 추가
            gr.update(visible=False), gr.update(visible=False), gr.update(visible=False),
            gr.update(visible=True),  gr.update(visible=True),  gr.update(visible=True),
        )

    confirmed = chosen["label"]
    det = (detail or "").strip()

    # ✅ 여기서 바로 표시용 Markdown 생성
    if det:
        md = f"**선택된 장소:** {confirmed}\n\n상세: {det}"
    else:
        md = f"**선택된 장소:** {confirmed}"

    return (
        "✅ 주소가 선택되었다.",
        confirmed, det, chosen["lat"], chosen["lng"],
        md,  # ✅ 추가
        gr.update(visible=True),  gr.update(visible=True),  gr.update(visible=True),
        gr.update(visible=False), gr.update(visible=False), gr.update(visible=False),
    )
