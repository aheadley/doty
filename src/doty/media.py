@multiprocessify
def proc_media(media_config, router_conn):
    setproctitle.setproctitle('doty: media worker')
    log = getWorkerLogger('media', level=LOG_LEVEL)
    keep_running = True
    log.debug('Media worker starting up')

    FFMPEG_CMD = lambda input_arg: [
        'ffmpeg',
        '-loglevel',    'quiet',
        '-hide_banner',
        '-y',
        '-re',
        '-i',           input_arg,
        '-ar',          str(PYMUMBLE_SAMPLERATE),
        '-ac',          '1',
        '-f',           's16le',
        '-'
    ]
    YOUTUBE_DL_CMD = lambda video_url: [
        'youtube-dl',
        '--quiet',
        '--no-warnings',
        '--no-progress',
        '--no-playlist',
        '--prefer-free-formats',
        '--output',     '-',
        video_url,
    ]

    ctx = {
        'youtube-dl': None,
        'ffmpeg': None,
        'txid': None,
    }
    volume = DEFAULT_MEDIA_VOLUME
    constrain_volume = lambda v: max(0.1, min(1.0, v))
    def adjust_volume(chunk, volume_ratio):
        d = numpy.frombuffer(chunk, dtype=numpy.int16) * volume_ratio
        return d.astype(dtype=numpy.int16, casting='unsafe').tobytes()

    def reset():
        if ctx['txid'] is not None:
            log.debug('Stopping txid=%s', ctx['txid'])
        if ctx['youtube-dl'] is not None:
            halt_process(ctx['youtube-dl'])
            ctx['youtube-dl'] = None
        if ctx['ffmpeg'] is not None:
            halt_process(ctx['ffmpeg'])
            ctx['ffmpeg'] = None
        ctx['txid'] = None


    log.info('Media worker running')
    while keep_running:
        if router_conn.poll(LONG_POLL):
            cmd_data = router_conn.recv()
            if cmd_data['cmd'] == MediaControlCommand.EXIT:
                log.debug('Recieved EXIT command from router')
                reset()
                keep_running = False
            elif cmd_data['cmd'] == MediaControlCommand.STOP:
                log.debug('Recieved STOP command from router')
                reset()
            elif cmd_data['cmd'] == MediaControlCommand.SET_VOLUME:
                volume = constrain_volume(cmd_data['value'])
                log.debug('Volume set to: %0.2f', volume)
            elif cmd_data['cmd'] == MediaControlCommand.SET_VOLUME_LOWER:
                volume = constrain_volume(volume - VOLUME_ADJUSTMENT_STEP)
                log.debug('Volume set to: %0.2f', volume)
            elif cmd_data['cmd'] == MediaControlCommand.SET_VOLUME_HIGHER:
                volume = constrain_volume(volume + VOLUME_ADJUSTMENT_STEP)
                log.debug('Volume set to: %0.2f', volume)
            elif cmd_data['cmd'] == MediaControlCommand.PLAY_AUDIO_URL:
                reset()
                ctx['txid'] = generate_uuid()
                log.info('PLAY_AUDIO_URL: txid=%s url=%s', ctx['txid'], cmd_data['url'])
                cmd = FFMPEG_CMD(cmd_data['url'])
                log.debug('Running cmd: %r', cmd)
                ctx['ffmpeg'] = subprocess.Popen(FFMPEG_CMD(cmd_data['url']),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )
            elif cmd_data['cmd'] == MediaControlCommand.PLAY_VIDEO_URL:
                reset()
                ctx['txid'] = generate_uuid()
                log.info('PLAY_VIDEO_URL: txid=%s url=%s', ctx['txid'], cmd_data['url'])
                cmd = YOUTUBE_DL_CMD(cmd_data['url'])
                log.debug('Running cmd: %r', cmd)
                ctx['youtube-dl'] = subprocess.Popen(cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )
                cmd = FFMPEG_CMD('-')
                log.debug('Running cmd: %r', cmd)
                ctx['ffmpeg'] = subprocess.Popen(cmd,
                    stdin=ctx['youtube-dl'].stdout,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )
            else:
                log.warning('Unrecognized command: %r', cmd_data)

        if ctx['youtube-dl'] is not None:
            if ctx['youtube-dl'].poll() is not None:
                log.debug('youtube-dl proc finished: txid=%s', ctx['txid'])
                ctx['youtube-dl'] = None

        chunk = None
        if ctx['ffmpeg'] is not None:
            chunk = ctx['ffmpeg'].stdout.read(PYMUMBLE_SAMPLERATE * SAMPLE_FRAME_SIZE)
            if ctx['ffmpeg'].poll() is not None:
                log.debug('ffmpeg proc finished: txid=%s', ctx['txid'])
                ctx['ffmpeg'] = None

            if chunk:
                router_conn.send({
                    'cmd': MediaControlCommand.AUDIO_CHUNK_READY,
                    'buffer': adjust_volume(chunk, volume),
                    'txid': ctx['txid'],
                })

    log.debug('Media worker process exiting')
