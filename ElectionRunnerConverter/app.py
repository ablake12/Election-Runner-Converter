import io
import re
from datetime import datetime
from tempfile import template
import pandas as pd
import streamlit as st
import requests
from io import StringIO

st.set_page_config(
    page_title="Election Runner CSV Generator", page_icon="🗳️", layout="centered"
)

st.title("🗳️ Election Runner CSV Generator")
st.write(
    "Upload the live meeting attendance sheet from EntriScan to generate an Election Runner import file containing only present members. You can then upload this file to Election Runner in the voters' section."
)


# Helper function to normalize names for reliable matching
def clean_name(name):
  # Explicitly check for nulls/NaNs
  if pd.isna(name):
    return ""
  # Ensure the value is strictly converted to a string
  name = str(name).strip().lower()
  # Remove extra internal spaces and punctuation
  name = re.sub(r"\s+", " ", name)
  name = re.sub(r"[^\w\s]", "", name)
  return name


def combine_exclusion_reasons(unmatched_df):
  if unmatched_df.empty:
    return unmatched_df.reindex(columns=["Member", "Reason"])

  unmatched_df = unmatched_df.drop_duplicates().copy()
  unmatched_df["_member_key"] = unmatched_df["Member"].apply(clean_name)
  missing_name_rows = unmatched_df["_member_key"] == ""
  unmatched_df.loc[missing_name_rows, "_member_key"] = [
    f"__missing_member_{index}"
    for index in unmatched_df.index[missing_name_rows]
  ]
  return (
    unmatched_df.groupby("_member_key", sort=False, as_index=False)
    .agg({
      "Member": "first",
      "Reason": lambda reasons: " | ".join(dict.fromkeys(reasons)),
    })
    .drop(columns=["_member_key"])
  )


# 1. Load the Master Template (Stored locally in the app repo or fetched from Google Sheets)
@st.cache_data
def load_master_template():
  # Your published Google Sheet CSV link
  template_path = st.secrets["template_sheets"]["csv_path"]

  print("Fetching latest member directory from Google Sheets...")
  response = requests.get(template_path, timeout=10)
  response.raise_for_status()
  df = pd.read_csv(StringIO(response.text))
  df.columns = df.columns.str.strip()

  columns_by_name = {column.lower(): column for column in df.columns}
  first_name_column = columns_by_name.get("first_name") or columns_by_name.get("first name")
  last_name_column = columns_by_name.get("last_name") or columns_by_name.get("last name")
  name_column = columns_by_name.get("name")

  voter_id_column = columns_by_name.get("voter_id") or columns_by_name.get("voter_identifier")
  required_columns = ["voter_key", "email", "vote_weight"]
  missing_columns = [column for column in required_columns if column not in columns_by_name]
  if voter_id_column is None:
    missing_columns.append("voter_id or voter_identifier")
  if not (first_name_column and last_name_column) and name_column is None:
    missing_columns.append("first_name and last_name, or name")

  if missing_columns:
    raise ValueError(
        "The Google Sheet is missing required columns: "
        + ", ".join(missing_columns)
    )

  if first_name_column and last_name_column:
    roster_names = (
        df[first_name_column].fillna("").astype(str)
        + " "
        + df[last_name_column].fillna("").astype(str)
    )
  else:
    roster_names = df[name_column]

  df = df.copy()
  email_column = columns_by_name["email"]
  vote_weight_column = columns_by_name["vote_weight"]
  df[vote_weight_column] = pd.to_numeric(
      df[vote_weight_column], errors="raise"
  ).fillna(1).astype("Int64")
  df["clean_full_name"] = roster_names.apply(clean_name)
  print(f"Successfully loaded {len(df)} voter records.")
  return df, template_path


def mark_master_template_confirmed():
  st.session_state["master_template_confirmed"] = True


@st.dialog("Before proceeding", on_dismiss=mark_master_template_confirmed)
def confirm_master_template_is_current():
  st.write("Make sure the latest sheet in the election runner template is up to date! You can make these updates directly in the Election Runner Google Sheet. This ensures that we have the latest member count in our chapter. You can access this sheet through the Technology committee's Google Drive. For any questions, please reach out to the Technology chairman or vice-chairman.")
  if st.button("Continue", type="primary"):
    st.session_state["master_template_confirmed"] = True
    st.rerun()


def cancel_member_removal():
  st.session_state.pop("pending_member_removal", None)


@st.dialog("Confirm member removal", on_dismiss=cancel_member_removal)
def confirm_member_removal():
  pending_removal = st.session_state.get("pending_member_removal")
  if pending_removal is None:
    return

  member_names = pending_removal["names"]
  st.write(f"Remove {len(member_names)} selected member(s) from the output?")
  st.dataframe(
    pd.DataFrame({"Members to remove": member_names}),
    #  height=min(300, 75 + 35 * (len(member_names) - 1)),
    height=min(300, 70 + 35 * (len(member_names) - 1)),
    hide_index=True,
  )
  cancel_col, confirm_col = st.columns(2)
  if cancel_col.button(
    "Cancel",
    key="cancel_member_removal",
    width="stretch",
  ):
    cancel_member_removal()
    st.rerun()
  if confirm_col.button(
    "Confirm removal",
    type="primary",
    key="confirm_member_removal",
    width="stretch",
  ):
    result = st.session_state["generated_result"]
    selected_rows = set(pending_removal["rows"])
    result["final_df"] = result["final_df"].iloc[
      [index for index in range(len(result["final_df"])) if index not in selected_rows]
    ].reset_index(drop=True)
    manual_exclusions = pd.DataFrame([
      {"Member": name, "Reason": "Removed manually by admin"}
      for name in member_names
    ])
    result["unmatched_df"] = combine_exclusion_reasons(pd.concat(
      [result["unmatched_df"], manual_exclusions],
      ignore_index=True,
    ))
    result_name_column = result["attendance_name_column"]
    existing_normalized_names = {
      record["clean_name"] for record in result["checked_out_records"]
    }
    for name in member_names:
      normalized_name = clean_name(name)
      if normalized_name not in existing_normalized_names:
        result["checked_out_records"].append({
          result_name_column: name,
          "clean_name": normalized_name,
        })
        existing_normalized_names.add(normalized_name)
      result["added_checked_out_names"].discard(normalized_name)
    st.session_state["generated_result"] = result
    cancel_member_removal()
    st.rerun()


if not st.session_state.get("master_template_confirmed", False):
  confirm_master_template_is_current()
  st.stop()


try:
  master_df, template_path = load_master_template()
except Exception as e:
  st.error(
      f"Error loading master template file: {e}. Please ensure the Google Sheet is accessible."
  )
  st.stop()

st.success(f"Master template loaded successfully: {len(master_df)} voter records.")
st.link_button("View master template", template_path)

# 2. File Uploader for Live Attendance
uploaded_file = st.file_uploader(
    "Upload Live Attendance CSV",
    type=["csv"],
    help="Upload the attendance export CSV for the current meeting from EntriScan.",
)

if uploaded_file is not None:
  attendance_df = pd.read_csv(uploaded_file)
  attendance_df.columns = attendance_df.columns.str.strip()
  attendance_columns = {column.lower(): column for column in attendance_df.columns}
  name_column = attendance_columns.get("name")
  group_column = attendance_columns.get("group")
  checked_out_column = attendance_columns.get("checked out at")

  if name_column is None or group_column is None or checked_out_column is None:
    st.error(
        "The attendance CSV must include 'Name', 'group', and 'Checked out at' columns."
    )
    st.stop()

  attendance_df["_dedupe_name"] = attendance_df[name_column].apply(clean_name)
  attendance_df["_dedupe_group"] = (
    attendance_df[group_column].fillna("").astype(str).str.strip().str.lower()
  )
  blank_name_rows = attendance_df["_dedupe_name"] == ""
  attendance_df.loc[blank_name_rows, "_dedupe_name"] = [
    f"__blank_name_{index}" for index in attendance_df.index[blank_name_rows]
  ]
  attendance_df["_has_checkout"] = attendance_df[checked_out_column].notna()
  original_attendance_count = len(attendance_df)
  attendance_df = (
    attendance_df.sort_values("_has_checkout", ascending=False, kind="stable")
    .drop_duplicates(subset=["_dedupe_name", "_dedupe_group"], keep="first")
    .sort_index()
    .drop(columns=["_dedupe_name", "_dedupe_group", "_has_checkout"])
  )
  duplicate_count = original_attendance_count - len(attendance_df)
  if duplicate_count:
    if duplicate_count == 1:
      st.info("Removed 1 duplicate attendance entry.")
    else:
      st.info(f"Removed {duplicate_count} duplicate attendance entries.")

  # Display a quick preview of uploaded raw data
  with st.expander(f"Preview Uploaded Attendance Data ({len(attendance_df)} attendees)"):
    st.dataframe(attendance_df, height=300, hide_index=True)

  if st.button("Generate Election Runner File", type="primary"):
    # Normalize names in attendance file
    attendance_df["clean_name"] = attendance_df[name_column].apply(clean_name)
    is_member = attendance_df[group_column].astype(str).str.strip().str.lower() == "member"
    is_checked_out = attendance_df[checked_out_column].notna()
    member_attendance = attendance_df[is_member]
    total_member_attendance_records = len(member_attendance)
    checked_out_members = member_attendance[is_checked_out.loc[member_attendance.index]]
    checked_out_names = set(checked_out_members["clean_name"]) - {""}
    active_members = member_attendance[
      ~is_checked_out.loc[member_attendance.index]
      & (member_attendance["clean_name"] != "")
      & ~member_attendance["clean_name"].isin(checked_out_names)
    ]
    present_names_set = set(active_members["clean_name"])

    master_names = set(master_df.loc[
      master_df["clean_full_name"] != "", "clean_full_name"
    ])
    email_column = next(
      column for column in master_df.columns if column.lower() == "email"
    )
    has_email = (
      master_df[email_column].notna()
      & master_df[email_column].astype(str).str.strip().ne("")
    )
    master_names_with_email = set(master_df.loc[
      has_email & (master_df["clean_full_name"] != ""), "clean_full_name"
    ])
    master_names_without_email = master_names - master_names_with_email
    unmatched_records = []
    is_guest = attendance_df[group_column].astype(str).str.strip().str.lower() == "guest"
    for _, row in attendance_df[is_guest].iterrows():
      guest_name = row[name_column]
      unmatched_records.append({
        "Member": guest_name if pd.notna(guest_name) and str(guest_name).strip() else "Name missing",
        "Reason": "This is a visitor who is not allowed to vote",
      })

    checked_out_records = (
      checked_out_members[[name_column, "clean_name"]]
      .drop_duplicates(subset=["clean_name"])
      .to_dict("records")
    )
    for record in checked_out_records:
      checked_out_name = record[name_column]
      unmatched_records.append({
        "Member": (
          checked_out_name
          if pd.notna(checked_out_name) and str(checked_out_name).strip()
          else "Name missing"
        ),
        "Reason": "Member checked out of meeting",
      })

    for _, row in member_attendance.iterrows():
      if row["clean_name"] == "":
        unmatched_records.append({"Member": "Name missing", "Reason": "Unknown"})
      elif row["clean_name"] not in master_names:
        unmatched_records.append({
          "Member": row[name_column],
          "Reason": "Unable to match their name",
        })
      elif (
        row["clean_name"] in master_names_without_email
        and row["clean_name"] not in master_names_with_email
      ):
        unmatched_records.append({
          "Member": row[name_column],
          "Reason": "Unable to retrieve email address",
        })

    unmatched_df = combine_exclusion_reasons(pd.DataFrame(
      unmatched_records,
      columns=["Member", "Reason"],
    ))

    # Filter master roster to keep only members who are present
    matched_df = master_df[
      (master_df["clean_full_name"] != "")
      & master_df["clean_full_name"].isin(present_names_set)
      & has_email
    ].copy()

    # Drop the temporary matching column
    final_df = matched_df.drop(columns=["clean_full_name"])

    st.session_state["generated_result"] = {
      "final_df": final_df,
      "unmatched_df": unmatched_df,
      "checked_out_records": checked_out_records,
      "attendance_name_column": name_column,
      "added_checked_out_names": set(),
      "total_attendance_records": total_member_attendance_records,
      "generated_at": datetime.now().strftime("%Y%m%d_%H%M%S"),
    }

if "generated_result" in st.session_state:
    result = st.session_state["generated_result"]
    final_df = result["final_df"]
    unmatched_df = result["unmatched_df"]
    total_attendance_records = result["total_attendance_records"]
    generated_at = result["generated_at"]
    result_name_column = result["attendance_name_column"]

    st.success(
        f"Processing Complete! Matched **{len(final_df)}** roster records against "
        f"**{total_attendance_records}** member attendance records."
    )
    add_back_warning = st.session_state.pop("add_back_warning", None)
    if add_back_warning:
      st.warning(add_back_warning)

    if not unmatched_df.empty:
      with st.container(border=True):
        if len(unmatched_df) == 1:
          st.warning(f"{len(unmatched_df)} attendee was excluded from the voter list:")
        else:
          st.warning(f"{len(unmatched_df)} attendees were excluded from the voter list:")
        st.dataframe(unmatched_df, hide_index=True, height=min(300, 38 + 35 * len(unmatched_df)))

    email_column = next(column for column in master_df.columns if column.lower() == "email")
    has_email = (
      master_df[email_column].notna()
      & master_df[email_column].astype(str).str.strip().ne("")
    )
    roster_names_with_email = set(
      master_df.loc[has_email, "clean_full_name"]
    )
    remaining_checked_out = [
      record for record in result["checked_out_records"]
      if record["clean_name"] not in result["added_checked_out_names"]
      and record["clean_name"] in roster_names_with_email
    ]
    if remaining_checked_out:
      with st.expander(f"Add Back Members ({len(remaining_checked_out)})"):
        st.caption("Add a member back to the voter output if they checked out of the meeting by mistake or were removed by mistake. This does not apply to visitors, members we weren't able to match, or those with unknown reasons.")
        add_back_names = [
          str(record[result_name_column]).strip()
          if pd.notna(record[result_name_column]) and str(record[result_name_column]).strip()
          else "Name missing"
          for record in remaining_checked_out
        ]
        add_back_emails = []
        for record in remaining_checked_out:
          member_emails = master_df.loc[
            (master_df["clean_full_name"] == record["clean_name"]) & has_email,
            email_column,
          ].drop_duplicates().astype(str).tolist()
          add_back_emails.append(", ".join(member_emails))

        add_back_review = pd.DataFrame({
          "Add back": [False] * len(remaining_checked_out),
          "Member": add_back_names,
          "Email": add_back_emails,
        })
        edited_add_back = st.data_editor(
          add_back_review,
          key=f"add_back_members_{generated_at}_{len(remaining_checked_out)}",
          column_config={
            "Add back": st.column_config.CheckboxColumn("Add back"),
          },
          disabled=["Member", "Email"],
          height=min(400, 70 + 35 * (len(remaining_checked_out) - 1)),
          hide_index=True,
          num_rows="fixed",
        )
        selected_add_back = [
          remaining_checked_out[index]
          for index, is_selected in enumerate(edited_add_back["Add back"])
          if is_selected
        ]
        if selected_add_back and st.button(
          f"Add anyways ({len(selected_add_back)})",
          key=f"add_back_selected_{generated_at}_{len(remaining_checked_out)}",
        ):
          member_frames = []
          added_names = set()
          failed_names = []
          for record in selected_add_back:
            normalized_name = record["clean_name"]
            member_rows = master_df[
              (master_df["clean_full_name"] == normalized_name) & has_email
            ].drop(columns=["clean_full_name"])
            if member_rows.empty:
              failed_names.append(
                str(record[result_name_column]).strip()
                if pd.notna(record[result_name_column])
                else "Name missing"
              )
              continue
            member_frames.append(member_rows)
            added_names.add(normalized_name)

          if member_frames:
            result["final_df"] = pd.concat(
              [result["final_df"], *member_frames], ignore_index=True
            ).drop_duplicates().reset_index(drop=True)
            added_display_names = {
              clean_name(record[result_name_column])
              for record in selected_add_back
              if record["clean_name"] in added_names
            }
            result["unmatched_df"] = result["unmatched_df"][
              ~(
                result["unmatched_df"]["Member"].apply(clean_name).isin(added_display_names)
                & result["unmatched_df"]["Reason"].isin([
                  "Member checked out of meeting",
                  "Removed manually by admin",
                ])
              )
            ].reset_index(drop=True)
            result["added_checked_out_names"].update(added_names)

          st.session_state["generated_result"] = result
          if failed_names:
            st.session_state["add_back_warning"] = (
              "Could not add these members because no matching roster record "
              "with an email address was found: " + ", ".join(failed_names)
            )
          st.rerun()

    with st.expander(f"Remove Members from Final Output ({len(final_df)})"):
      if final_df.empty:
        st.caption("There are no members in the output.")
      else:
        st.caption("Remove any members who shouldn't be in election runner from the final output before downloading the file.")
        output_columns = {column.lower(): column for column in final_df.columns}
        first_name_column = output_columns.get("first_name") or output_columns.get("first name")
        last_name_column = output_columns.get("last_name") or output_columns.get("last name")
        output_name_column = output_columns.get("name")
        if first_name_column and last_name_column:
          output_names = (
            final_df[first_name_column].fillna("").astype(str).str.strip()
            + " "
            + final_df[last_name_column].fillna("").astype(str).str.strip()
          ).str.strip()
        else:
          output_names = final_df[output_name_column].fillna("").astype(str).str.strip()
        output_emails = final_df[output_columns["email"]].fillna("").astype(str).str.strip()
        member_filter = st.text_input(
          "Filter by name or email",
          key=f"remove_member_filter_{generated_at}",
        ).strip().casefold()
        matching_rows = [
          row_index
          for row_index, (member_name, member_email) in enumerate(
            zip(output_names, output_emails)
          )
          if member_filter in f"{member_name} {member_email}".casefold()
        ]
        if matching_rows:
          member_review = pd.DataFrame({
            "Remove": [False] * len(matching_rows),
            "Member": [
              output_names.iloc[index] or "Name missing"
              for index in matching_rows
            ],
            "Email": [output_emails.iloc[index] for index in matching_rows],
          })
          edited_members = st.data_editor(
            member_review,
            key=f"member_removal_{generated_at}_{len(final_df)}_{member_filter}",
            column_config={
              "Remove": st.column_config.CheckboxColumn("Remove"),
            },
            disabled=["Member", "Email"],
            height=min(400, 75 + 35 * (len(matching_rows) - 1)),
            hide_index=True,
            num_rows="fixed",
          )
          selected_rows = [
            matching_rows[index]
            for index, is_selected in enumerate(edited_members["Remove"])
            if is_selected
          ]
          if selected_rows and st.button(
            f"Remove selected ({len(selected_rows)})",
            icon=":material/delete:",
            key=f"remove_selected_{generated_at}_{len(final_df)}",
          ):
            st.session_state["pending_member_removal"] = {
              "rows": selected_rows,
              "names": [
                output_names.iloc[index] or "Name missing"
                for index in selected_rows
              ],
            }
            st.rerun()
        else:
          st.caption("No members match that filter.")

    st.subheader(f"Preview Final Output ({len(final_df)} members)")
    st.dataframe(final_df, height=300, hide_index=True)

    # Convert DataFrame to downloadable CSV in memory
    csv_buffer = io.BytesIO()
    final_df.to_csv(csv_buffer, index=False)
    csv_bytes = csv_buffer.getvalue()

    st.download_button(
        label="📥 Download Election Runner Import CSV",
        data=csv_bytes,
        file_name=f"election_runner_voters_ready_{generated_at}.csv",
        mime="text/csv",
    )

if "pending_member_removal" in st.session_state:
  confirm_member_removal()