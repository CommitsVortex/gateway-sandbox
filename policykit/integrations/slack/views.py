import datetime
import json
import logging
import requests
from django.conf import settings
from django.contrib.auth import authenticate, login
from django.shortcuts import redirect, render
from integrations.slack.models import (
    SlackCommunity,
    SlackJoinConversation,
    SlackPinMessage,
    SlackPostMessage,
    SlackRenameConversation,
    SlackStarterKit,
    SlackUser,
)
from integrations.slack.utils import get_slack_user_fields
from policyengine.models import Community, CommunityRole, LogAPICall, PlatformActionBundle

logger = logging.getLogger(__name__)


def slack_login(request):
    """redirect after metagov has gotten the slack user token"""
    logger.debug(f"slack_login: {request.GET}")

    if request.GET.get("error"):
        return redirect(f"/login?error={request.GET.get('error')}")

    user_token = request.GET.get("user_token")
    user_id = request.GET.get("user_id")
    team_id = request.GET.get("team_id")
    user = authenticate(request, user_token=user_token, user_id=user_id, team_id=team_id)
    if user:
        login(request, user)
        return redirect("/main")

    # Note: this is not always an accurate error message.
    return redirect("/login?error=policykit_not_yet_installed_to_that_community")


def slack_install(request):
    logger.debug(f"Slack installation completed: {request.GET}")
    expected_state = request.session.get("community_install_state")
    if expected_state is None or request.GET.get("state") is None or (not request.GET.get("state") == expected_state):
        logger.error(f"expected {expected_state}")
        return redirect("/login?error=bad_state")

    if request.GET.get("error"):
        return redirect(f"/login?error={request.GET.get('error')}")

    # metagov identifier for the "parent community" to install Slack to
    metagov_community_slug = request.GET.get("community")

    # TODO(issue): stop passing user id and token
    user_id = request.GET.get("user_id")
    user_token = request.GET.get("user_token")

    try:
        community = Community.objects.get(metagov_slug=metagov_community_slug)
    except Community.DoesNotExist:
        logger.error(f"Community not found: {metagov_community_slug}")
        return redirect("/login?error=community_not_found")

    # Get team info from Slack
    response = requests.post(
        f"{settings.METAGOV_URL}/api/internal/action/slack.method",
        json={"parameters": {"method_name": "team.info"}},
        headers={"X-Metagov-Community": metagov_community_slug},
    )
    if not response.ok:
        return redirect("/login?error=server_error")
    data = response.json()
    team = data["team"]
    team_id = team["id"]
    readable_name = team["name"]

    # Set readable_name for Community
    if not community.readable_name:
        community.readable_name = readable_name
        community.save()

    user_group, _ = CommunityRole.objects.get_or_create(
        role_name="Base User", name="Slack: " + readable_name + ": Base User"
    )

    slack_community = SlackCommunity.objects.filter(team_id=team_id).first()
    if slack_community is None:
        logger.debug(f"Creating new SlackCommunity under {community}")
        slack_community = SlackCommunity.objects.create(
            community=community,
            community_name=readable_name,
            team_id=team_id,
            base_role=user_group,
        )
        user_group.community = slack_community
        user_group.save()

        # get the list of users, create SlackUser object for each user
        logger.debug(f"Fetching user list for {slack_community}...")
        response = LogAPICall.make_api_call(slack_community, {}, "users.list")
        for new_user in response["members"]:
            if (not new_user["deleted"]) and (not new_user["is_bot"]) and (new_user["id"] != "USLACKBOT"):
                u, _ = SlackUser.objects.get_or_create(
                    username=new_user["id"],
                    readable_name=new_user["real_name"],
                    avatar=new_user["profile"]["image_24"],
                    is_community_admin=new_user["is_admin"],
                    community=slack_community,
                )
                if user_token and user_id and new_user["id"] == user_id:
                    logger.debug(f"Storing access_token for installing user ({user_id})")
                    u.access_token = user_token
                    u.save()

        context = {
            "starterkits": [kit.name for kit in SlackStarterKit.objects.all()],
            "community_name": slack_community.community_name,
            "creator_token": user_token,
            "platform": "slack",
        }
        return render(request, "policyadmin/init_starterkit.html", context)

    else:
        logger.debug("community already exists, updating name..")
        slack_community.community_name = readable_name
        slack_community.save()
        slack_community.community.readable_name = readable_name
        slack_community.community.save()

        # Store token for the user who (re)installed Slack
        if user_token and user_id:
            installer = SlackUser.objects.filter(community=slack_community, username=user_id).first()
            if installer is not None:
                logger.debug(f"Storing access_token for installing user ({user_id})")
                installer.access_token = user_token
                installer.save()
            else:
                logger.debug(f"User '{user_id}' is re-installing but no SlackUser exists for them, creating one..")
                response = slack_community.make_call("users.info", {"user": user_id})
                user_info = response["user"]
                user_fields = get_slack_user_fields(user_info)
                user_fields["password"] = user_token
                user_fields["access_token"] = user_token
                SlackUser.objects.create(
                    community=slack_community,
                    username=user_info["id"],
                    defaults=user_fields,
                )

        return redirect("/login?success=true")


def is_policykit_action(community, test_a, test_b, api_name):
    current_time_minus = datetime.datetime.now() - datetime.timedelta(seconds=2)

    logs = LogAPICall.objects.filter(community=community, proposal_time__gte=current_time_minus, call_type=api_name)
    if logs.exists():
        # logger.debug(f"Made {logs.count()} calls to {api_name} in the last 2 seconds")
        for log in logs:
            j_info = json.loads(log.extra_info)
            # logger.debug(j_info)
            if test_a == j_info[test_b]:
                return True

    return False


def maybe_create_new_api_action(community, outer_event):
    new_api_action = None
    event_type = outer_event["event_type"]
    initiator = outer_event.get("initiator").get("user_id")
    if not initiator:
        # logger.debug(f"{event_type} event does not have an initiating user ID, skipping")
        return

    event = outer_event["data"]
    if event_type == "message" and event.get("subtype") == "channel_name":
        if not is_policykit_action(community, event["name"], "name", SlackRenameConversation.ACTION):
            new_api_action = SlackRenameConversation(
                community=community, name=event["name"], channel=event["channel"], previous_name=event["old_name"]
            )
            u, _ = SlackUser.objects.get_or_create(username=initiator, community=community)
            new_api_action.initiator = u
    elif event_type == "message" and event.get("subtype") == None:
        if not is_policykit_action(community, event["text"], "text", SlackPostMessage.ACTION):
            new_api_action = SlackPostMessage()
            new_api_action.community = community
            new_api_action.text = event["text"]
            new_api_action.channel = event["channel"]
            new_api_action.timestamp = event["ts"]

            u, _ = SlackUser.objects.get_or_create(username=initiator, community=community)

            new_api_action.initiator = u

    elif event_type == "member_joined_channel":
        if not is_policykit_action(community, event["channel"], "channel", SlackJoinConversation.ACTION):
            new_api_action = SlackJoinConversation()
            new_api_action.community = community
            if event.get("inviter"):
                u, _ = SlackUser.objects.get_or_create(username=event["inviter"], community=community)
                new_api_action.initiator = u
            else:
                u, _ = SlackUser.objects.get_or_create(username=initiator, community=community)
                new_api_action.initiator = u
            new_api_action.users = initiator
            new_api_action.channel = event["channel"]

    elif event_type == "pin_added":
        if not is_policykit_action(community, event["channel_id"], "channel", SlackPinMessage.ACTION):
            new_api_action = SlackPinMessage()
            new_api_action.community = community

            u, _ = SlackUser.objects.get_or_create(username=initiator, community=community)
            new_api_action.initiator = u
            new_api_action.channel = event["channel_id"]
            new_api_action.timestamp = event["item"]["message"]["ts"]

    return new_api_action


def default_election_vote_message(policy):
    return (
        "This action is governed by the following policy: " + policy.description + ". Decide between options below:\n"
    )


def default_boolean_vote_message(policy):
    return (
        "This action is governed by the following policy: "
        + policy.description
        + ". Vote with :thumbsup: or :thumbsdown: on this post."
    )


def post_policy(policy, action, users=[], post_type="channel", template=None, channel=None):
    payload = {"callback_url": f"{settings.SERVER_URL}/metagov/internal/outcome/{action.pk}"}

    if action.action_type == "PlatformActionBundle" and action.bundle_type == PlatformActionBundle.ELECTION:
        payload["poll_type"] = "choice"
        payload["title"] = template or default_election_vote_message(policy)
        payload["options"] = [str(a) for a in action.bundled_actions.all()]
    else:
        payload["poll_type"] = "boolean"
        payload["title"] = template or default_boolean_vote_message(policy)

    if channel is None:
        # Determine wich channel to post in
        if post_type == "channel":
            if action.action_type == "PlatformAction" and hasattr(action, "channel"):
                channel = action.channel
            elif action.action_type == "PlatformActionBundle":
                first_action = action.bundled_actions.all()[0]
                if hasattr(first_action, "channel"):
                    channel = first_action.channel
        # For "mpim" (multi-persom im), open a private conversation among participants, to post the vote in.
        if post_type == "mpim" and users is not None and len(users) > 0:
            usernames = ",".join([user.username for user in users])
            response = LogAPICall.make_api_call(policy.community, {"users": usernames}, "conversations.open")
            channel = response["channel"]["id"]

    if channel is None:
        raise Exception("Failed to determine which channel to post in")

    payload["channel"] = channel

    # Kick off process in Metagov
    logger.debug(f"Starting slack vote on {action} governed by {policy}. Payload: {payload}")
    response = requests.post(
        f"{settings.METAGOV_URL}/api/internal/process/slack.emoji-vote",
        json=payload,
        headers={"X-Metagov-Community": policy.community.metagov_slug},
    )
    if not response.ok:
        raise Exception(f"Error starting process: {response.status_code} {response.reason} {response.text}")
    location = response.headers.get("location")
    if not location:
        raise Exception("Response missing location header")

    # Store location URL of the process, so we can use it to close the Metagov process when policy evaluation "completes"
    action.proposal.governance_process_url = f"{settings.METAGOV_URL}{location}"
    action.proposal.save()

    # Get the unique 'ts' of the vote post, and save it on the action
    response = requests.get(action.proposal.governance_process_url)
    if not response.ok:
        raise Exception(f"{response.status_code} {response.reason} {response.text}")
    process = response.json()
    ts = process["outcome"]["message_ts"]
    action.community_post = ts
    action.save()
    logger.debug(f"Saved action with '{ts}' as community_post, and process at {action.proposal.governance_process_url}")
