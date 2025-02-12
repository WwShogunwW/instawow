<script lang="ts">
  import { faChevronCircleDown, faChevronCircleUp } from "@fortawesome/free-solid-svg-icons";
  import { getContext } from "svelte";
  import { type Addon } from "../api";
  import { API_KEY, type Api } from "../stores/api.svelte";
  import Icon from "./SvgIcon.svelte";

  const api = getContext<Api>(API_KEY);

  let {
    selections,
    folders,
    choices,
    idx,
    expanded = false,
  } = $props<{
    selections: Addon[];
    folders: { name: string; version: string }[];
    choices: Addon[];
    idx: number;
    expanded?: boolean;
  }>();

  let selectionIdx = $state(0);

  let selection = $derived(choices[selectionIdx]);
  let installedVersion = $derived(folders.find((f) => f.version)?.version || "");

  $effect(() => {
    selections[idx] = choices[selectionIdx];
  });
</script>

<div class="addon-stub">
  <div class="header">
    <div class="folders">
      <span class="main-folder">{folders[0].name}</span>
      {#if folders.length > 1}
        <span class="remaining-folders">
          {folders
            .slice(1)
            .map((f) => f.name)
            .join(", ")}
        </span>
      {/if}
    </div>
    {#if !choices.length}
      <p class="unreconciled-message">no matches found</p>
    {/if}
  </div>
  {#if choices.length}
    <details class="selection-controls" open={expanded}>
      <summary>
        <div class="selection-grid">
          <div aria-label="installed version" class="defn-or-version">
            {installedVersion}
          </div>
          <!-- prettier-ignore -->
          <div aria-label="selection" class="defn-or-version">
            ({choices.length})
            {#if selection}
              {selection.source}:{selection.slug}==<span title={selection.date_published}>{selection.version}</span>
            {:else}
              skip
            {/if}
          </div>
          <div>
            <Icon class="icon icon-collapsed" icon={faChevronCircleDown} />
            <Icon class="icon icon-expanded" icon={faChevronCircleUp} />
          </div>
        </div>
      </summary>
      <ul class="selection-grid choices">
        {#each choices as choice, choiceIdx}
          <li>
            <input
              type="radio"
              id="addon-selection-{idx}-{choiceIdx}"
              value={choiceIdx}
              bind:group={selectionIdx}
            />
            <label for="addon-selection-{idx}-{choiceIdx}">
              <span class="defn-or-version">{choice.source}:{choice.slug}=={choice.version}</span>
              <button
                role="link"
                onclick={(e) => {
                  e.stopPropagation();
                  api.openUrl(choice.url);
                }}
              >
                [↗]
              </button>
            </label>
          </li>
        {/each}
        <li>
          <input
            type="radio"
            id="addon-selection-{idx}-skip"
            value={-1}
            bind:group={selectionIdx}
          />
          <label for="addon-selection-{idx}-skip">
            <span class="defn-or-version">skip</span>
          </label>
        </li>
      </ul>
    </details>
  {/if}
</div>

<style lang="scss">
  @use "scss/vars";

  .addon-stub {
    transition: all 0.2s;
  }

  .header,
  .selection-controls {
    padding: 0.4em 0.75em;
  }

  .header {
    display: flex;
    flex-direction: row;
    align-items: baseline;
    justify-content: space-between;
    gap: 1rem;
  }

  .folders {
    overflow-x: hidden;
    white-space: nowrap;
    text-overflow: ellipsis;
    font-weight: 700;

    &:only-child {
      line-height: 1.5rem;
    }

    .remaining-folders {
      font-size: 0.8em;
      color: var(--inverse-color-tone-b);
    }
  }

  .defn-or-version {
    font-family: vars.$mono-font-stack;
    font-size: 0.7rem;
  }

  .selection-controls {
    margin-top: -0.25rem;
    padding-top: 0;
    color: var(--inverse-color-tone-b);

    .selection-grid {
      display: grid;
      grid-template-columns: 1fr 2fr 1rem;
      column-gap: 0.5rem;
    }

    summary {
      list-style: none;
      line-height: 1rem;

      &::-webkit-details-marker,
      &::marker {
        display: none;
      }

      :nth-child(2) {
        padding: 0 0.2rem;
      }

      :last-child {
        justify-self: right;
      }

      :global(.icon) {
        display: block;
        height: 1rem;
        width: 1rem;
        fill: var(--inverse-color-tone-b);
      }
    }

    &[open] :global(.icon-collapsed) {
      display: none !important;
    }

    &:not([open]) :global(.icon-expanded) {
      display: none !important;
    }
  }

  .choices {
    @extend %unstyle-list;

    li {
      @extend .defn-or-version;
      display: flex;
      padding: 0 0.2rem;
      grid-column-start: 2;
      line-height: 1rem;

      &:first-child {
        margin-top: 0.18rem;
        padding-top: 0.18rem;
        border-top: 1px solid var(--inverse-color-tone-b);
      }

      label {
        flex-grow: 1;

        &::before {
          content: "( ) ";
        }
      }
    }

    [type="radio"] {
      display: none;

      &:checked + label::before {
        content: "(x) ";
      }
    }
  }

  .unreconciled-message {
    margin: 0;
    font-size: 0.75em;
    color: var(--inverse-color-tone-b);
  }
</style>
