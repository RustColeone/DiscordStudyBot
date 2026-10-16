import os
import math

from providers import clip_query as clipQuery
from app.bot_types import BotAttachment, BotResponse, HandlerResult, IncomingMessage
from parsers.command_parsers import parse_clip_command


class ClipFeature:
    def can_handle(self, message: IncomingMessage) -> bool:
        return message.content.startswith("$clip")

    async def handle(self, app, message: IncomingMessage) -> HandlerResult:
        cmd = parse_clip_command(message.content)
        if cmd.errors:
            return [BotResponse(text="❌ " + "\n".join(cmd.errors))]

        channel_id = message.channel_id
        premium_tier = message.metadata.get("guild_premium_tier", 0)

        if cmd.cancel:
            clipQuery.clear_pending_clips(channel_id)
            return [BotResponse(text="🗑️ Cancelled pending clips")]

        if cmd.confirm:
            return await self._handle_confirm(channel_id, cmd, premium_tier)

        has_quality_settings = any([cmd.resolution, cmd.fps, cmd.bitrate, cmd.output_format])
        if cmd.clip_index is not None or (has_quality_settings and not cmd.urls):
            return await self._handle_update(channel_id, cmd, premium_tier)

        if cmd.urls:
            return await self._handle_create(channel_id, cmd, premium_tier)

        return [BotResponse(text="❌ No parameters provided")]

    async def _handle_confirm(self, channel_id: str, cmd, premium_tier: int) -> HandlerResult:
        pending = clipQuery.get_pending_clips(channel_id)
        if not pending:
            return [BotResponse(text="❌ No pending clips to process")]

        clips_to_process = [clip for index, clip in enumerate(pending) if index not in cmd.skip_indices]
        if not clips_to_process:
            return [BotResponse(text="❌ All clips were skipped")]

        if cmd.keep_file is not None:
            for clip in clips_to_process:
                clip.keep_file = cmd.keep_file

        responses = [BotResponse(text=f"🎬 Processing {len(clips_to_process)} clip(s)... (this may take some time)")]
        max_size = clipQuery.get_discord_size_limit(premium_tier)
        responses.extend(await self._build_clip_file_responses(clips_to_process, max_size))
        clipQuery.clear_pending_clips(channel_id)
        if not any(response.metadata.get("tool_status") == "failed" or (response.text or "").startswith("❌")
                   for response in responses):
            responses.append(BotResponse(text="✅ All clips processed"))
        return responses

    async def _handle_update(self, channel_id: str, cmd, premium_tier: int) -> HandlerResult:
        pending = clipQuery.get_pending_clips(channel_id)
        if not pending:
            return [BotResponse(text="❌ No pending clips to modify")]

        clip_index = cmd.clip_index if cmd.clip_index is not None else 0
        if clip_index >= len(pending):
            return [BotResponse(text=f"❌ Invalid clip index. You have {len(pending)} pending clip(s)")]

        updates = {}
        if cmd.resolution:
            updates["resolution"] = cmd.resolution
        if cmd.fps:
            updates["fps"] = cmd.fps
        if cmd.bitrate:
            updates["bitrate"] = cmd.bitrate
        if cmd.output_format:
            updates["output_format"] = cmd.output_format
        if cmd.keep_file is not None:
            updates["keep_file"] = cmd.keep_file

        clipQuery.update_clip_setting(channel_id, clip_index, **updates)
        clip = pending[clip_index]
        duration = clip.end - clip.start
        max_size = clipQuery.get_discord_size_limit(premium_tier)
        quality_options = clipQuery.get_quality_options(duration, max_size, clip.output_format or "mp4")
        status = "✅" if clip.estimated_size_mb <= max_size else "⚠️"

        changes = []
        if cmd.resolution:
            changes.append(f"resolution → {cmd.resolution}")
        if cmd.fps:
            changes.append(f"fps → {cmd.fps}")
        if cmd.bitrate:
            changes.append(f"bitrate → {cmd.bitrate}")
        if cmd.output_format:
            changes.append(f"format → {cmd.output_format}")
        if cmd.keep_file is not None:
            changes.append(f"keep-file → {cmd.keep_file}")

        lines = [f"📊 Updated Clip {clip_index + 1}:"]
        if changes:
            lines.append(f"Changes: {', '.join(changes)}")
        lines.append(f"Settings: {clip.resolution} @ {clip.bitrate} ({clip.fps}fps)")
        lines.append(f"{status} **Estimated size: {clip.estimated_size_mb:.2f}MB** (limit: {max_size}MB)")
        lines.append("")
        lines.append("Quality options:")
        for option in quality_options:
            fit = "✅" if option.estimated_size_mb <= max_size else "❌"
            lines.append(
                f"{option.label}) {option.resolution} @ {option.bitrate} ({option.fps}fps) → ~{option.estimated_size_mb:.2f}MB {fit}"
            )

        return [BotResponse(text="\n".join(lines))]

    async def _handle_create(self, channel_id: str, cmd, premium_tier: int) -> HandlerResult:
        max_size = clipQuery.get_discord_size_limit(premium_tier)
        clips = []

        for index, url in enumerate(cmd.urls):
            try:
                title, site, full_duration = await clipQuery.get_video_info(url)

                start = clipQuery.parse_time(cmd.starts[index]) if index < len(cmd.starts) else 0
                end = clipQuery.parse_time(cmd.ends[index]) if index < len(cmd.ends) else full_duration

                if end <= start:
                    return [BotResponse(text=f"❌ End time must be after start time for URL: {url}")]

                if end > full_duration:
                    end = full_duration

                duration = end - start
                default_format = cmd.output_format or "mp4"
                quality_options = clipQuery.get_quality_options(duration, max_size, default_format)
                best_option = quality_options[0] if quality_options else clipQuery.QualityOption("720p", "1500k", 30, 0, "A")

                clip = clipQuery.ClipSpec(
                    url=url,
                    start=start,
                    end=end,
                    resolution=cmd.resolution or best_option.resolution,
                    fps=cmd.fps or best_option.fps,
                    bitrate=cmd.bitrate or best_option.bitrate,
                    output_format=default_format,
                    video_title=title,
                    source_site=site,
                    keep_file=bool(cmd.keep_file),
                    range_explicit=index < len(cmd.starts) or index < len(cmd.ends),
                )

                is_audio = default_format.lower() in ["mp3", "m4a", "wav", "aac", "ogg", "flac"]
                clip.estimated_size_mb = clipQuery.estimate_clip_size(
                    duration,
                    clip.resolution,
                    clip.bitrate,
                    clip.fps,
                    not (default_format == "gif" or is_audio),
                )
                clips.append(clip)
            except Exception as error:
                return [self._failure(f"Error processing URL {url}: {error}", error)]

        if not clips:
            return [BotResponse(text="❌ No valid clips to process")]

        if cmd.force:
            responses = [BotResponse(text=f"🎬 Processing {len(clips)} clip(s) immediately...")]
            responses.extend(await self._build_clip_file_responses(clips))
            return responses

        clipQuery.store_pending_clips(channel_id, clips)
        return [BotResponse(
            text=self._build_preview_message(clips, max_size),
            metadata={
                "clip_preview": {
                    "limit_mb": max_size,
                    "clips": [self._clip_summary(clip) for clip in clips],
                }
            },
        )]

    def pending_agent_context(self, channel_id: str, premium_tier: int) -> str:
        clips = clipQuery.get_pending_clips(channel_id) or []
        if not clips:
            return ""
        max_size = clipQuery.get_discord_size_limit(premium_tier)
        summaries = [self._clip_summary(clip) for clip in clips]
        return (
            "Pending clip task (structured state): "
            f"Discord upload limit is {max_size}MB; clips={summaries}. "
            "Resolve the user's latest answer against this pending task. Use adjust_pending_clip to trim or compress, "
            "manage_pending_clips to confirm/cancel, and ask_clarification only when the user's intended goal remains ambiguous."
        )

    @staticmethod
    def _failure(message, error=None):
        source_error = isinstance(error, clipQuery.VideoSourceError)
        return BotResponse(text="❌ " + message, metadata={
            "tool_status": "failed", "retryable": False,
            "error_code": "source_unavailable" if source_error else "processing_failed",
        })

    async def download_for_agent(self, message, arguments):
        """Download the requested range within the destination and user file budgets."""
        try:
            progress = message.metadata.get("tool_progress")
            if progress is not None:
                await progress("Inspecting the video source and duration...")
            title, site, full_duration = await clipQuery.get_video_info(arguments["url"])
            start = clipQuery.parse_time(arguments["start"]) if arguments.get("start") else 0
            end = clipQuery.parse_time(arguments["end"]) if arguments.get("end") else full_duration
            end = min(end, full_duration)
            if start < 0 or end <= start:
                return [self._failure("The requested video range is invalid")]
            channel_budget = float(message.metadata.get('upload_limit_mb') or
                                   clipQuery.get_discord_size_limit(message.metadata.get("guild_premium_tier", 0)))
            requested_budget = float(arguments.get('max_size_mb', channel_budget))
            if not math.isfinite(requested_budget) or requested_budget <= 0:
                return [self._failure('The requested maximum file size must be positive and finite')]
            max_size = min(channel_budget, requested_budget)
            budget_source = ('user_target' if requested_budget < channel_budget else
                             message.metadata.get('upload_limit_source', 'channel_limit'))
            audio_choices = [arguments['audio_bitrate']] if arguments.get('audio_bitrate') else ['128k', '96k', '64k']
            fit = None
            for audio_bitrate in audio_choices:
                fit = clipQuery.calculate_upload_fit(end - start, max_size, audio_bitrate)
                if fit is not None:
                    break
            if fit is None:
                return [BotResponse(
                    text=(f'The estimated bitrate for {end-start:g}s does not fit the {max_size:g} MB file budget '
                          f'({budget_source}) at the supported quality floor. No rendering or upload was attempted. '
                          'Ask whether to allow a larger file or choose a shorter range; this is not evidence of a platform rejection.'),
                    metadata={"tool_status": "needs_input", "error_code": "compression_budget",
                              'file_budget': {'max_mb': max_size, 'source': budget_source},
                              'render_attempted': False, 'upload_attempted': False},
                )]
            resolution, bitrate, fps = fit
            explicit_quality = any(arguments.get(key) for key in ('fps', 'resolution', 'bitrate', 'audio_bitrate'))
            fps = arguments.get("fps") or fps
            clip = clipQuery.ClipSpec(
                url=arguments["url"], start=start, end=end,
                resolution=arguments.get("resolution") or resolution,
                bitrate=arguments.get("bitrate") or bitrate, fps=fps, output_format="mp4",
                video_title=title, source_site=site, audio_bitrate=audio_bitrate,
                copy_budget_mb=max_size if start == 0 and end >= full_duration and not explicit_quality else None,
            )
            encoding = {
                "duration_seconds": end - start, "fps": fps, "resolution": clip.resolution,
                "video_bitrate": clip.bitrate, "audio_bitrate": clip.audio_bitrate, "upload_limit_mb": max_size,
                'budget_source': budget_source,
                "estimated_size_mb": clipQuery.estimate_clip_size(end - start, clip.resolution, clip.bitrate, fps,
                                                                  audio_bitrate=clip.audio_bitrate),
            }
            if progress is not None:
                await progress(f"Keeping {end - start:g}s; an H.264 source that fits {max_size:g} MB ({budget_source}) is copied as is, "
                               f"otherwise encoding at {fps} fps, video {clip.bitrate} + audio {clip.audio_bitrate}, "
                               f"estimated {encoding['estimated_size_mb']:.2f} MB."
                               if clip.copy_budget_mb else
                               f"Keeping {end - start:g}s at {fps} fps; video {clip.bitrate} + audio {clip.audio_bitrate}, "
                               f"estimated {encoding['estimated_size_mb']:.2f} MB / {max_size:g} MB file budget ({budget_source}).")
            responses = await self._build_clip_file_responses([clip], max_size, progress)
            if clip.stream_copy:
                copied = clip.stream_copy
                encoding.update(method='stream_copy', fps=copied['fps'], resolution=f"{copied['width']}x{copied['height']}",
                                video_bitrate='source', audio_bitrate='source', video_codec=copied['video_codec'],
                                audio_codec=copied['audio_codec'], estimated_size_mb=copied['estimated_size_mb'])
                settings = (f"{copied['fps']} fps; {copied['width']}x{copied['height']}; source H.264/AAC copied without re-encoding")
            else:
                encoding['method'] = 'reencode'
                settings = f"{fps} fps; {clip.resolution}; video {clip.bitrate} + audio {clip.audio_bitrate}"
            for response in responses:
                response.metadata["video_encoding"] = encoding
                # The next character turn receives measured settings, rather than inventing a bitrate calculation.
                response.text = (response.text or "") + (
                    f"\nRequested range: {end - start:g}s; {settings}; estimated {encoding['estimated_size_mb']:.2f} MB; "
                    f"file budget {max_size:g} MB ({budget_source}). The rendered file size is checked separately.")
            return responses
        except Exception as error:
            return [self._failure(str(error), error)]

    async def adjust_pending_for_agent(self, message: IncomingMessage, arguments) -> HandlerResult:
        channel_id = message.channel_id
        clips = clipQuery.get_pending_clips(channel_id) or []
        clip_index = int(arguments.get("clip", 1)) - 1
        if clip_index < 0 or clip_index >= len(clips):
            return [BotResponse(text="❌ No matching pending clip to adjust")]

        clip = clips[clip_index]
        action = arguments["action"]
        max_size = clipQuery.get_discord_size_limit(message.metadata.get("guild_premium_tier", 0))
        if action == "trim":
            if not arguments.get("start") or not arguments.get("end"):
                return [BotResponse(text="❌ Trimming requires both a start and end time")]
            start = clipQuery.parse_time(arguments["start"])
            end = min(clipQuery.parse_time(arguments["end"]), clip.end)
            if start < 0 or end <= start:
                return [BotResponse(text="❌ End time must be after start time")]
            clipQuery.update_clip_setting(channel_id, clip_index, start=start, end=end)
        elif action == "fit_discord":
            duration = clip.end - clip.start
            fit_settings = clipQuery.calculate_discord_fit(duration, max_size)
            if fit_settings is None:
                return [BotResponse(text=(
                    "⚠️ The full video is too long to fit the Discord limit at usable video quality. "
                    "Please choose a shorter start and end time."
                ))]
            resolution, bitrate, fps = fit_settings
            clipQuery.update_clip_setting(
                channel_id,
                clip_index,
                resolution=arguments.get("resolution") or resolution,
                bitrate=bitrate,
                fps=fps,
            )
        elif action == "keep_full_quality":
            clipQuery.update_clip_setting(channel_id, clip_index, keep_file=True)

        preview = self._build_preview_message(clips, max_size)
        note = ""
        if action == "keep_full_quality" and clip.estimated_size_mb > max_size:
            note = (
                "\n\n⚠️ This remains too large for Discord upload. It can be processed and retained on the bot host, "
                "but trimming or compression is required for Discord delivery."
            )
        return [BotResponse(
            text=preview + note,
            metadata={
                "clip_preview": {
                    "limit_mb": max_size,
                    "clips": [self._clip_summary(item) for item in clips],
                }
            },
        )]

    @staticmethod
    def _clip_summary(clip) -> dict:
        return {
            "title": clip.video_title,
            "start": clip.start,
            "end": clip.end,
            "duration": clip.end - clip.start,
            "resolution": clip.resolution,
            "bitrate": clip.bitrate,
            "format": clip.output_format,
            "estimated_size_mb": clip.estimated_size_mb,
            "keep_file": clip.keep_file,
            "range_explicit": clip.range_explicit,
        }

    async def _build_clip_file_responses(self, clips, max_size=None, progress=None) -> HandlerResult:
        responses = []
        for index, clip in enumerate(clips, start=1):
            temp_path = None
            try:
                extension = clip.output_format or "mp4"
                temp_path = clipQuery.allocate_clip_output_path(clip, index)

                if progress is not None:
                    success = await clipQuery.create_clip(clip, temp_path, progress=progress)
                else:
                    success = await clipQuery.create_clip(clip, temp_path)
                if success and os.path.exists(temp_path):
                    file_size_mb = os.path.getsize(temp_path) / (1024 * 1024)
                    keep_note = " (kept)" if clip.keep_file else ""
                    if max_size is not None and file_size_mb > max_size:
                        if clip.keep_file:
                            responses.append(BotResponse(text=(
                                f"📹 Clip {index}/{len(clips)}: {clip.video_title} ({file_size_mb:.2f}MB, kept on host). "
                                f"The result exceeds the configured {max_size:g}MB file budget, so it was not attached."
                            ), metadata={'tool_status':'failed','error_code':'output_budget_exceeded','retryable':False}))
                        else:
                            os.remove(temp_path)
                            responses.append(BotResponse(text=(
                                f"❌ Clip {index} rendered at {file_size_mb:.2f}MB, above the configured {max_size:g}MB file budget. "
                                "It was deleted; adjust compression or the requested range before another attempt."
                            ), metadata={'tool_status':'failed','error_code':'output_budget_exceeded','retryable':False}))
                        continue
                    responses.append(
                        BotResponse(
                            text=f"📹 Clip {index}/{len(clips)}: {clip.video_title} ({file_size_mb:.2f}MB){keep_note}",
                            attachment=BotAttachment(
                                path=temp_path,
                                filename=f"clip_{index}.{extension}",
                                delete_after_send=not clip.keep_file,
                            ),
                        )
                    )
                else:
                    responses.append(self._failure(f"Failed to process clip {index}"))
                    if temp_path and os.path.exists(temp_path):
                        os.remove(temp_path)
            except Exception as error:
                responses.append(self._failure(f"Error processing clip {index}: {error}", error))
                if temp_path and os.path.exists(temp_path):
                    os.remove(temp_path)
        return responses

    def _build_preview_message(self, clips, max_size: int) -> str:
        lines = [f"📊 **Clip Preview** (Discord limit: {max_size}MB)", ""]
        for index, clip in enumerate(clips, start=1):
            duration = clip.end - clip.start
            status = "✅" if clip.estimated_size_mb <= max_size else "⚠️"
            lines.append(f"**Clip {index}:** {status}")
            lines.append(f"Source: {clip.video_title} - {clip.source_site}")
            lines.append(
                f"Duration: {duration:.1f}s ({clipQuery.format_time(clip.start)} → {clipQuery.format_time(clip.end)})"
            )
            lines.append(f"Selected: {clip.resolution} @ {clip.bitrate} ({clip.fps}fps)")
            lines.append(f"Estimated: **{clip.estimated_size_mb:.2f}MB**")
            lines.append(f"Keep file after send: {'yes' if clip.keep_file else 'no'}")
            lines.append("Options:")
            for option in clipQuery.get_quality_options(duration, max_size, clip.output_format):
                fit = "✅" if option.estimated_size_mb <= max_size else "❌"
                lines.append(
                    f"  {option.label}) {option.resolution} @ {option.bitrate} ({option.fps}fps) → ~{option.estimated_size_mb:.2f}MB {fit}"
                )
            lines.append("")

        lines.append("**Commands:**")
        lines.append("`$clip --confirm` - Process all clips")
        lines.append("`$clip --clip <N> --resolution 720p` - Adjust clip N")
        lines.append("`$clip --confirm --skip <N>` - Skip clip N")
        lines.append("`$clip --keep-file` - Keep generated files after sending")
        lines.append("`$clip --delete-file` - Delete generated files after sending")
        lines.append("`$clip --cancel` - Cancel all")
        return "\n".join(lines)
