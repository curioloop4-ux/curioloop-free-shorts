            print(f"OUTPUT_ONLY: preview ready at {path}; upload skipped.")
            # Advance only the in-memory cursor so this preview run can render
            # multiple distinct clips per artist without persisting upload history.
            source_state[item["id"]] = {
                "name": item["name"],
                "next_clip": selected_index + 1,
                "complete": selected_index >= len(clips),
                "clips": clips
            }
            continue

        video_id = upload(youtube, path, title, description, tags, privacy)
        uploaded_count += 1

        source_state[item["id"]] = {
            "name": item["name"],
            "next_clip": selected_index + 1,
            "complete": selected_index >= len(clips),
            "clips": clips
        }
        history["last_source_id"] = item["id"]
        history["sources"] = source_state
        history["processed"] = [
            sid for sid, state in source_state.items() if state.get("complete")
        ][-2000:]
        HISTORY.parent.mkdir(parents=True, exist_ok=True)
        HISTORY.write_text(
            json.dumps(history, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8"
        )
        print(f"Recorded successful upload {video_id}; progress saved immediately.")

    if output_only:
        print("OUTPUT_ONLY run finished; no uploads or progress changes were made.")
    else:
        print(f"FANCAMVERSE batch complete: {uploaded_count}/{batch_size} Shorts uploaded.")

if __name__ == "__main__":
    main()