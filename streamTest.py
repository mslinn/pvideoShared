# FIXME Reads 2,928,640 bytes and stops.
# Perhaps ffmpeg's STDIN not properly connected to previous process's STDOUT?
#
# Ctrl-C shows:
# File "streamTest.py", line 243, in <module>
#   lambda_handler(test_event, TestContext())
# File "streamTest.py", line 222, in lambda_handler
#   return personalize(user_name, video_url)
# File "streamTest.py", line 196, in personalize
#   ffmpeg_process.stdin.write(data)
# KeyboardInterrupt

# This program uses AWS CLI when deployed as an AWS Lambda,
# but does not require an AWS account or AWS CLI if executed from the command line


import boto3, botocore, json, logging, os, platform, sys
from subprocess import CalledProcessError, Popen, PIPE
from sys import stderr, stdout

logger = logging.getLogger()
logger.setLevel(logging.INFO)

command_line = False # used to keep track of how this program was invoked
buffer_size = 10240   # what is the optimal buffer size?


def log_cmd(cmd):
    """ Side effect: Logs cmd to be executed to STDERR in such a way that it can be pasted into the REPL
    :param cmd: command array to log
    :return: nothing
    """
    cmd_str = ""
    for c in cmd:
        if " " in c:
            d = "\"{}\"".format(c)
        else:
            d = c

        if c == cmd[-1]:
            e = "{}\n".format(d)
        else:
            e = "{} \\\n  ".format(d)

        cmd_str += e
    logger.info('About to invoke {}'.format(cmd_str))


def s3_file_as_stream(bucket, key):
    """
    Read video from AWS S3 bucket and return a stream.
    FIXME incomplete, does not work, not used
    """
    s3 = boto3.resource('s3')
    response = s3.Object(bucket_name=bucket, key=key).get()
    body = response['Body']
    stream = body.x
    return stream


def ffmpeg_cmd(user_name):
    """
    Read from STDIN (pipe:0) and write to STDOUT (pipe:1)
    AWS Lambdas do not have access to networking so ffmpeg's ability to stream in from a URL cannot be used.
    Instead,videos are read from an AWS S3 bucket and streamed to ffmpeg vis STDIN, picked up here as pipe:0
    :param user_name: User name to be overlaid into first 5 seconds of video
    """
    enable = "enable"
    font_file = "fontfile='fonts/DejaVuSerif.ttf'"
    font = "fontsize=48: fontcolor=black: {0}".format(font_file)
    first_5_seconds = "{0}='lt(t,5)'".format(enable)
    box = "drawbox=x=0: y=0: w=1920: h=135: t=68: color=yellow@1: {0}".format(first_5_seconds)
    draw_text = "drawtext=text='Licensed to': x='(main_w-text_w)/2': y=20: {0}: {1}, " \
                "drawtext=text='{2}': x='(main_w-text_w)/2': y=80: {3}: {4}".format(font, first_5_seconds, user_name,
                                                                                    font, first_5_seconds)

    if platform.system().startswith("CYGWIN"):
        ffmpeg = "/usr/local/bin/ffmpeg" # recent build enabling most ffmpeg functions needs to be installed
    elif platform.system() == "Windows":
        ffmpeg = "C:/PROGRA~2/FFMPEG~1/ffmpeg.exe" # recent build enabling most ffmpeg functions needs to be installed
    elif platform.system() == "darwin":
        ffmpeg = "/usr/local/bin/ffmpeg" # recent build enabling most ffmpeg functions needs to be installed
    else: # aws lambda environment assumed, use ffmpeg bundled with this program
        ffmpeg = "bin/ffmpeg"

    cmd = [
        ffmpeg,
        "-i", 'pipe:0',
        "-v", "warning",
        "-strict", "experimental",
        "-vf", '{0}, {1}'.format(box, draw_text),
        "-y",
        "-f", "mp4",
        "-movflags", "frag_keyframe",
        "pipe:1"
    ]
    log_cmd(cmd)
    return cmd


def s3_url(video_url):
    """
    Converts an URL (using http or https protocols) for a video stored in an AWS S3 bucket that is configured to act
    as a web server into the s3:// protocol understood by AWS CLI
    :param video_url: URL of video in a bucket
    :return: video URL using s3:// protocol
    """
    from urlparse import urlparse
    parsed_url = urlparse(video_url)
    bucket = parsed_url.netloc[: -len(".s3.amazonaws.com")]
    key = parsed_url.path[1:]
    return "s3://{0}/{1}".format(bucket, key)


def s3_cat_cmd(video_s3):
    """
    Copy video to STDOUT.
    If running as an AWS Lambda, fetch video from S3 bucket using "aws s3 cp" , else fetch from URL using "wget".
    :param video_s3: URL of video to be copied, specified with s3:// protocol
    :return: streamed video
    """
    if platform.system().startswith("CYGWIN"):
        aws = "/bin/aws"
    elif platform.system() == "Windows":
        aws = "c:/Python27/Scripts/aws.cmd"
    elif platform.system() == "darwin":
        aws = "/usr/local/bin/aws"
    else: # assume running as AWS Lambda so use our bundled Debian-compatible image
        aws = "bin/aws"

    if command_line:
        cmd = [
            "wget", "-qO", "-", video_s3
        ]
    else:
        cmd = [
            aws, "s3", "cp", s3_url(video_s3), "-"
        ]

    log_cmd(cmd)
    return cmd


def make_stdin_unbuffered():
    """
    One way of causing STDIN to be unbuffered
    :return: nothing
    """
    if not platform.system().startswith("CYGWIN") and platform.system() != "Windows":
        from fcntl import fcntl, F_GETFL, F_SETFL
        from os import O_NONBLOCK, read
        flags = fcntl(p.stdout, F_GETFL)  # get current p.stdout flags
        fcntl(p.stdout, F_SETFL, flags | O_NONBLOCK)


class Unbuffered(object):
    """
    Another way of causing STDIN to be unbuffered, as a side effect
    """
    def __init__(self, stream):
        self.stream = stream

    def write(self, data):
        self.stream.write(data)
        self.stream.flush()

    def __getattr__(self, attr):
        return getattr(self.stream, attr)


def make_stdout_unbuffered():
    """
    One way of causing STDOUT to be unbuffered, as a side effect
    :return: nothing
    """
    # sys.stdout = os.fdopen(sys.stdout.fileno(), 'w', 0)  # reopen sys.stdout in unbuffered mode
    sys.stdout = Unbuffered(sys.stdout)


def personalize(user_name, video_url):
    """
    Transform video with the given URL so it has the first 5 seconds overlaid with generated graphics.
    STDOUT from cat_process is piped into STDIN of ffmpeg_process, buffer_size bytes at a time.
    cat_process streams the video at video_url to STDOUT.
    ffmpeg_process the streamed video from cat_process from STDIN, processes the video, and streams the output to STDOUT.
    FIXME: only a portion of the video is processed. Something is wrong in the way that I connected the streams.
    :param user_name: Name to be imprinted on video
    :param video_url: URL of video to transform
    :return: Streamed video, sent to STDOUT
    """
    try:
        make_stdin_unbuffered()
        make_stdout_unbuffered()
        cat_process = Popen(s3_cat_cmd(video_url), stdout=PIPE)
        ffmpeg_process = Popen(ffmpeg_cmd(user_name), stdin=PIPE, stdout=PIPE)
        total_bytes = 0L
        while True:
            data = cat_process.stdout.read(buffer_size)
            if not data:
                cat_process.stdout.close()
                ffmpeg_process.stdout.close()
                if command_line:
                    stderr.write("\nDone!\n")
                break
            else:
                if command_line:
                    total_bytes += len(data)
                    stderr.write("\r\x1B[KRead {:,} bytes".format(total_bytes))
                ffmpeg_process.stdin.write(data)
                ffmpeg_process.stdout.flush()
        # cat_process.stdout.close()  # should this be here?
        # ffmpeg_process.stdout.close()  # should this be here?
        return ffmpeg_process.stdout
    except CalledProcessError as e:
        print(e.output)
        return 0  # not sure what to return so I punted


def lambda_handler(event, context):
    """
    Entry point when invoked as an AWS Lambda.
    :param event: Incoming event to process containing parameters
    :param context: Ignored
    :return: STDOUT (streamed video generated by personalize function). Hopefully STDOUT won't be buffered.
    """
    user_name = event['userName']
    video_url = event['videoUrl']

    if command_line:
        stderr.write("Running from command line so HTTP headers will not be emitted\r\n")
    else:
        stdout.write('Content-Type: video/mp4\r\n')
        stdout.write('Content-Disposition: inline; filename="{0}\r\n'.format("streamTest.mp4"))
        stdout.write('\r\n')
    return personalize(user_name, video_url)


if __name__ == "__main__":
    """
    Entry point when run from command line. Sets command_line flag.
    """
    class TestContext(object):  # fake object
        function_name = "++MAIN++"

    logging.basicConfig()

    test_video_url = "https://courseassets.scalacourses.com/1/html/ScalaCore/assets/videos/tx/course_scalaIntermediate_Web.mp4"

    test_event = {
        "userName": "Fred Flintstone",
        "videoUrl": test_video_url
    }
    command_line = True
    lambda_handler(test_event, TestContext())
